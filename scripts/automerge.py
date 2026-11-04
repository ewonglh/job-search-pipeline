#!/usr/bin/env python3
"""Autopilot merge lane: merge reviewer-APPROVEd open app/* PRs and enqueue.

Policy (owner directive 2026-10-22, "auto tailor all; parallelise the tailor
and auto merge approved prs. but we also want to avoid sending duplicate
applications"):

- The CV approval gate moves to the reviewer: an APPROVE verdict in
  ``review_state.json`` authorises the merge of an open ``app/*`` tailoring
  PR. Unreviewed / REVISE / REJECT / error PRs are never touched.
- The narrative gate (essays, cover letters, short answers) is unaffected and
  stays owner-only at the drain; this script never sees submission content.
- Duplicate-application guards run on every candidate, in this order:
  1. enqueue's own guards (``queue.py enqueue``): idempotent by PR number,
     ``already submitted in tracker`` from applications.yaml, and the
     secondary ``active_key`` guard (normalized posting link or company|role)
     against every unfinished queue row. This is the authoritative filter —
     the checks below only pre-screen candidates so obviously-dead PRs are
     not merged at all.
  2. ``skipped.yaml`` ledger: a candidate whose PR number OR canonical
     posting link is recorded there is a re-proposal of a deliberate owner
     skip — never merged.
  3. Duplicate-family rule: at most one PR per normalized posting link may
     be merged by this lane per run, and never when an earlier PR for the
     same link is already merged (the merged one feeds the queue; the open
     twin gets left for the audit lane to close).

The script never comments, closes, or edits on GitHub; it merges and
enqueues. Every merge is followed by ``queue.py enqueue`` (the enqueue step
is the dedupe backstop) and a verification read-back of each PR state.

Usage:
  python3 automerge.py            # evaluate + merge + enqueue
  python3 automerge.py --dry-run  # report candidates, touch nothing
"""
import argparse
import json
import os
import re
import subprocess
import sys

SERVICE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SERVICE, "repo")
GH_REPO = os.environ.get("JOBSEARCH_GH_REPO", "example-user/resume")
REVIEW_STATE = os.path.join(SERVICE, "review_state.json")
SKIPPED_YAML = os.path.join(SERVICE, "skipped.yaml")


def log(*a):
    print(*a, file=sys.stderr)


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def gh_json(args):
    proc = run(["gh"] + args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    return json.loads(proc.stdout or "null")


def normalize_url(url):
    import urllib.parse
    p = urllib.parse.urlsplit(str(url or "").strip())
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, p.path.rstrip("/"), "", "")).lower()


def load_reviewed():
    with open(REVIEW_STATE, encoding="utf-8") as fh:
        state = json.load(fh)
    reviewed = state.get("reviewed")
    if not isinstance(reviewed, dict):
        raise SystemExit("automerge: review_state.json has no reviewed map — hard stop")
    return reviewed


def load_skips():
    """Return (skip_pr_numbers, skip_links) from the durable skip ledger."""
    prs, links = set(), set()
    try:
        import yaml
        with open(SKIPPED_YAML, encoding="utf-8") as fh:
            entries = yaml.safe_load(fh) or []
    except FileNotFoundError:
        return prs, links
    for e in entries:
        if not isinstance(e, dict):
            continue
        n = e.get("pr_number")
        try:
            if n:
                prs.add(int(n))
        except (TypeError, ValueError):
            pass
        link = normalize_url(e.get("link", ""))
        if link:
            links.add(link)
    return prs, links


def load_merged_pr_index():
    """Normalized posting link -> earliest merged PR number (via bodies)."""
    prs = gh_json(["pr", "list", "--repo", GH_REPO, "--state", "merged",
                   "--limit", "500", "--json", "number,body,mergedAt,headRefName"])
    idx = {}
    for pr in prs:
        m = re.search(r"https?://\S+", str(pr.get("body") or ""))
        if not m:
            continue
        link = normalize_url(m.group(0).rstrip(").,;"))
        idx.setdefault(link, int(pr["number"]))
    return idx


def candidate_links(pr):
    """Best-effort posting link(s) for an open PR: body first, then the
    work/<slug>.json sidecar for the branch slug (bodies on old PRs may omit
    the URL entirely)."""
    links = []
    body = str(pr.get("body") or "")
    m = re.search(r"https?://\S+", body)
    if m:
        links.append(normalize_url(m.group(0).rstrip(").,")))
    slug = re.sub(r"^app/", "", pr.get("headRefName") or "")
    for cand in (os.path.join(SERVICE, "work", slug + ".json"),):
        if os.path.isfile(cand):
            try:
                with open(cand, encoding="utf-8") as fh:
                    link = normalize_url(json.load(fh).get("link", ""))
                if link and link not in links:
                    links.append(link)
            except (ValueError, OSError):
                pass
    return [l for l in links if l]


def open_approved_prs(reviewed):
    """Open app/* PRs whose verdict is approve, oldest first.
    (Owner directive 2026-10-24: verdict "retailored" is NOT merge-eligible --
    the review lane re-reviews retailor-fixed PRs; only a fresh approve merges.)"""
    prs = gh_json(["pr", "list", "--repo", GH_REPO, "--state", "open",
                   "--limit", "1000", "--json",
                   "number,title,headRefName,createdAt,body,url"])
    out = []
    for pr in prs:
        if not str(pr.get("headRefName") or "").startswith("app/"):
            continue
        entry = reviewed.get(str(pr["number"]))
        if not entry or entry.get("verdict") != "approve":
            continue
        out.append(pr)
    out.sort(key=lambda p: str(p.get("createdAt") or ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report candidates and guards; merge nothing")
    args = ap.parse_args()

    reviewed = load_reviewed()
    skip_prs, skip_links = load_skips()
    merged_index = load_merged_pr_index()
    candidates = open_approved_prs(reviewed)

    if not candidates:
        log("automerge: no open APPROVEd app/* PRs")
        return 0

    # Guard 2 (skip ledger) + Guard 3 (duplicate family) pre-screen.
    picked, blocked = [], []
    seen_links = set()
    for pr in candidates:
        n = int(pr["number"])
        links = candidate_links(pr)
        why = None
        if n in skip_prs:
            why = "PR number is in skipped.yaml (owner skip)"
        elif any(l in skip_links for l in links):
            why = "posting link is in skipped.yaml (owner skip)"
        else:
            for l in links:
                if l in seen_links:
                    why = f"duplicate of a PR already picked this run ({l})"
                    break
                earlier = merged_index.get(l)
                if earlier and earlier != n:
                    why = f"posting link already merged as PR #{earlier}"
                    break
        if why:
            blocked.append((n, why))
        else:
            picked.append(pr)
            seen_links.update(links)

    for n, why in blocked:
        log(f"automerge: guard-block #{n}: {why}")
    if not picked:
        log("automerge: nothing to merge after guards")
        return 0

    if args.dry_run:
        for pr in picked:
            print(f"would-merge #{pr['number']} {pr['title'][:70]}")
        return 0

    merged_ok = []
    for pr in picked:
        n = pr["number"]
        proc = run(["gh", "pr", "merge", str(n), "--repo", GH_REPO, "--merge"])
        if proc.returncode != 0:
            log(f"automerge: merge failed #{n}: {proc.stderr.strip()[:120]}")
            continue
        chk = gh_json(["pr", "view", str(n), "--repo", GH_REPO,
                       "--json", "state,mergedAt"])
        if chk.get("state") != "MERGED":
            log(f"automerge: #{n} did not reach MERGED (state={chk.get('state')})")
            continue
        merged_ok.append(n)
        log(f"automerge: merged #{n}")

    if not merged_ok:
        log("automerge: no merges completed")
        return 1

    # Enqueue is the authoritative duplicate backstop; run it once for the batch.
    proc = run([sys.executable, os.path.join(SERVICE, "queue.py"), "enqueue"])
    log(proc.stdout.strip() or "(enqueue produced no rows)")
    if proc.returncode != 0:
        log("automerge: enqueue exited non-zero — inspect queue state")
        return 1
    print("merged:", ",".join(str(n) for n in merged_ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
