#!/usr/bin/env python3
"""Readiness report for reviewed app/* PRs awaiting the owner's GitHub merge.

The helper checks that an approval covers the current PR head, master resume,
job description, profile, and artifact hashes. It rejects incomplete reviewer
findings and unavailable or malformed duplicate, skip, queue, tracker, and cap
inputs. Narrative approval remains owner-only at the drain.

The script performs no GitHub writes and never enqueues. A direct owner merge
is observed later by ``queue.py enqueue`` as the authorization event.

Usage:
  python3 automerge.py            # report readiness; owner merges in GitHub
  python3 automerge.py --dry-run  # same read-only report
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.parse

from review_contract import approval_result, canonical_sha256
from config import (default_repo_dir, default_runtime_path, github_repo_setting,
                    path_setting)

SERVICE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = path_setting("JOBSEARCH_REPO", default_repo_dir())
GH_REPO = github_repo_setting("JOBSEARCH_GH_REPO", "your-user/your-resume-repo")
REVIEW_STATE = path_setting("JOBSEARCH_REVIEW_STATE",
                            default_runtime_path("review_state.json"))
SKIPPED_YAML = path_setting("JOBSEARCH_SKIPPED",
                            default_runtime_path("skipped.yaml"))
WORK_DIR = path_setting("JOBSEARCH_WORK", default_runtime_path("work"))


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
    try:
        p = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return ""
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", "")).lower()


def _strip_yaml_comment(line):
    quote = None
    out = []
    for char in line:
        if char in "\"'":
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        if char == "#" and quote is None:
            break
        out.append(char)
    return "".join(out)


def strict_yaml_rows(text, label):
    """Accept the repo's flat list-of-mappings YAML subset, rejecting ambiguity."""
    rows, current, saw_empty = [], None, False
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise ValueError(f"{label}:{number}: tabs are not supported")
        clean = _strip_yaml_comment(raw).rstrip()
        stripped = clean.strip()
        if not stripped or stripped in ("---", "..."):
            continue
        if stripped == "[]":
            if rows or current is not None or saw_empty:
                raise ValueError(f"{label}:{number}: mixed empty and populated YAML")
            saw_empty = True
            continue
        if saw_empty:
            raise ValueError(f"{label}:{number}: content after empty list")
        if clean.startswith("- "):
            current = {}
            rows.append(current)
            item = clean[2:].strip()
        elif clean.startswith("  ") and not clean.startswith("   ") and current is not None:
            item = clean[2:].strip()
        else:
            raise ValueError(f"{label}:{number}: expected a flat list mapping")
        if ":" not in item:
            raise ValueError(f"{label}:{number}: expected key: value")
        key, value = item.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ValueError(f"{label}:{number}: invalid field name")
        if key in current:
            raise ValueError(f"{label}:{number}: duplicate field {key}")
        # Nested data and YAML anchors are outside the state-file contract.
        if value.strip().startswith(("&", "*", "!", "{", ">", "|")):
            raise ValueError(f"{label}:{number}: unsupported YAML value")
        current[key] = value.strip()
    return [] if saw_empty else rows


def _unquote(value):
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == "'":
            return value[1:-1].replace("''", "'")
        try:
            return json.loads(value)
        except ValueError as exc:
            raise ValueError("malformed quoted YAML value") from exc
    return value


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains a duplicate object key")
        result[key] = value
    return result


def load_reviewed():
    with open(REVIEW_STATE, encoding="utf-8") as fh:
        state = json.load(fh, object_pairs_hook=_unique_json_object)
    if not isinstance(state, dict) or not isinstance(state.get("reviewed"), dict):
        raise ValueError("review_state.json must contain a reviewed map")
    reviewed = state["reviewed"]
    seen = set()
    for key, entry in reviewed.items():
        if not isinstance(key, str) or not key.isdigit() or int(key) < 1 or str(int(key)) != key:
            raise ValueError("review_state.json has an invalid PR identity")
        if int(key) in seen:
            raise ValueError("review_state.json has ambiguous PR identities")
        seen.add(int(key))
        if not isinstance(entry, dict):
            raise ValueError("review_state.json has a malformed review entry")
    return reviewed


def load_skips():
    """Return (skip_pr_numbers, skip_links) from the durable skip ledger."""
    prs, links = set(), set()
    with open(SKIPPED_YAML, encoding="utf-8") as fh:
        text = fh.read()
    rows = strict_yaml_rows(text, "skipped.yaml")
    for entry in rows:
        raw_number = _unquote(entry.get("pr_number", ""))
        if raw_number and not re.fullmatch(r"\d+", raw_number):
            raise ValueError("skipped.yaml has an invalid PR number")
        number = int(raw_number) if raw_number else 0
        if number < 0:
            raise ValueError("skipped.yaml has an invalid PR number")
        supplied_link = _unquote(entry.get("link", ""))
        link = normalize_url(supplied_link)
        if supplied_link and not link:
            raise ValueError("skipped.yaml has an invalid posting link")
        if number and number in prs:
            raise ValueError("skipped.yaml has duplicate PR identities")
        if link and link in links:
            raise ValueError("skipped.yaml has duplicate posting links")
        if not number and not link:
            raise ValueError("skipped.yaml has an entry with no posting identity")
        if number:
            prs.add(number)
        if link:
            links.add(link)
    return prs, links


def load_merged_pr_index():
    """Normalized posting link -> earliest merged PR number (via bodies)."""
    prs = gh_json(["pr", "list", "--repo", GH_REPO, "--state", "merged",
                   "--limit", "1000", "--json", "number,body,mergedAt,headRefName"])
    if not isinstance(prs, list):
        raise ValueError("merged PR list is malformed")
    if len(prs) >= 1000:
        raise ValueError("merged PR list may be truncated")
    idx = {}
    numbers = set()
    for pr in prs:
        if not isinstance(pr, dict):
            raise ValueError("merged PR list contains a malformed entry")
        try:
            number = int(pr.get("number"))
        except (TypeError, ValueError):
            raise ValueError("merged PR list contains an invalid number")
        if number < 1 or number in numbers:
            raise ValueError("merged PR list has ambiguous PR identities")
        numbers.add(number)
        if not str(pr.get("headRefName") or "").startswith("app/"):
            continue
        body_links = set()
        for match in re.finditer(r"https?://[^\s)\]<>{\"']+", str(pr.get("body") or "")):
            link = normalize_url(match.group(0).rstrip(".,;"))
            if link and "github.com" not in urllib.parse.urlsplit(link).netloc:
                body_links.add(link)
        if len(body_links) != 1:
            raise ValueError("merged app PR has missing or ambiguous posting identity")
        link = next(iter(body_links))
        if link in idx and idx[link] != number:
            raise ValueError("merged PR list contains a duplicate posting")
        idx[link] = number
    return idx


def candidate_links(pr, work_item):
    """Resolve one posting identity and reject disagreement with the PR body."""
    work_link = normalize_url(work_item.get("link", ""))
    if not work_link:
        raise ValueError("work item has no valid posting link")
    body = str(pr.get("body") or "")
    body_links = set()
    for match in re.finditer(r"https?://[^\s)\]<>{\"']+", body):
        link = normalize_url(match.group(0).rstrip(".,;"))
        if link and "github.com" not in urllib.parse.urlsplit(link).netloc:
            body_links.add(link)
    if body_links and body_links != {work_link}:
        raise ValueError("PR body and work item have ambiguous posting links")
    return [work_link]


def _load_service_module(name, filename):
    path = os.path.join(SERVICE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load " + filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_work_item(pr):
    branch = str(pr.get("headRefName") or "")
    match = re.fullmatch(r"app/([A-Za-z0-9][A-Za-z0-9._-]*)", branch)
    if not match:
        raise ValueError("PR branch has an invalid application identity")
    path = os.path.join(WORK_DIR, match.group(1) + ".json")
    with open(path, encoding="utf-8") as fh:
        item = json.load(fh, object_pairs_hook=_unique_json_object)
    if not isinstance(item, dict):
        raise ValueError("work item is malformed")
    return item


def _strict_rows(text, label, module):
    raw = strict_yaml_rows(text, label)
    parsed = module.parse_yaml_list(text)
    if not isinstance(parsed, list) or len(parsed) != len(raw):
        raise ValueError(label + " could not be parsed completely")
    if any(not isinstance(row, dict) for row in parsed):
        raise ValueError(label + " must contain mappings")
    return parsed


def _git_output(repo, *args):
    proc = run(["git", "-C", repo] + list(args), timeout=30)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip() or "git provenance check failed")
    return proc.stdout.strip()


def _git_blob_sha256(repo, revision, path):
    proc = subprocess.run(["git", "-C", repo, "show", revision + ":" + path],
                          capture_output=True, timeout=30)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ValueError(detail or "review artifact is unavailable")
    return hashlib.sha256(proc.stdout).hexdigest()


def preflight_candidate(pr, review, work_item=None, queue_module=None,
                        tailor_module=None, queue_file=None, gates_file=None,
                        repo_dir=None):
    """Fail closed on snapshot provenance, caps, queue duplicates and tracker state."""
    q = queue_module or _load_service_module("jobsearch_queue_for_automerge", "queue.py")
    tailor = tailor_module or _load_service_module("jobsearch_tailor_for_automerge", "tailor.py")
    work_item = work_item if work_item is not None else load_work_item(pr)
    try:
        valid, reason = approval_result(review, str(pr.get("headRefOid") or ""))
        if not valid:
            return False, reason
        links = candidate_links(pr, work_item)
        posting_link = links[0]
        provenance = review["provenance"]
        if normalize_url(review["audit"].get("posting_url", "")) != posting_link:
            return False, "review posting identity differs from the work item"
        if not isinstance(work_item.get("jd_text"), str) or not work_item["jd_text"].strip():
            return False, "current job description is unavailable"
        jd_hash = hashlib.sha256(work_item["jd_text"].encode("utf-8")).hexdigest()
        profile = work_item.get("profile")
        if not isinstance(profile, dict):
            return False, "current profile is unavailable"
        if provenance.get("current_jd_sha256") != jd_hash:
            return False, "review is for a different job description"
        if provenance.get("profile_sha256") != canonical_sha256(profile):
            return False, "review is for a different profile"
        if work_item.get("master") != "master/resume.tex":
            return False, "work item has an unexpected master resume path"

        repo = repo_dir or q.REPO_DIR
        if not os.path.isdir(repo) or not os.path.exists(os.path.join(repo, ".git")):
            return False, "resume repository clone is unavailable"
        fetched = q._git(repo, "fetch", "--quiet", "--no-tags", "origin", "main")
        if fetched.returncode != 0:
            return False, "current resume repository state is unavailable"
        master_blob = _git_output(repo, "rev-parse", "origin/main:master/resume.tex")
        source_blob = _git_output(
            repo, "rev-parse", provenance["master_commit"] + ":master/resume.tex")
        if master_blob != provenance["master_resume_blob"] or source_blob != master_blob:
            return False, "master resume provenance is stale or mismatched"
        profile_text = _git_output(repo, "show", "origin/main:" + tailor.PROFILE_PATH)
        current_profile = tailor.parse_yaml_map(profile_text)
        if canonical_sha256(current_profile) != provenance["profile_sha256"]:
            return False, "current profile differs from the reviewed profile"
        pr_number = int(pr["number"])
        fetched = q._git(repo, "fetch", "--quiet", "--no-tags", "origin",
                         "refs/pull/{}/head".format(pr_number))
        if fetched.returncode != 0:
            return False, "current PR head contents are unavailable"
        fetched_head = _git_output(repo, "rev-parse", "FETCH_HEAD")
        if fetched_head != pr.get("headRefOid"):
            return False, "fetched PR head changed during readiness check"
        if _git_output(repo, "rev-parse", "FETCH_HEAD:master/resume.tex") != master_blob:
            return False, "PR head contains a different master resume"
        if _git_output(repo, "show", "FETCH_HEAD:" + tailor.PROFILE_PATH) != profile_text:
            return False, "PR head contains a different profile"
        for artifact, digest in provenance["artifacts"].items():
            if _git_blob_sha256(repo, "FETCH_HEAD", artifact) != digest:
                return False, "PR artifact hash mismatch: " + artifact

        qfile = queue_file or q.QUEUE_FILE
        gfile = gates_file or tailor.GATE_FILE
        if not os.path.isfile(qfile):
            return False, "queue snapshot is unavailable"
        with open(qfile, encoding="utf-8") as fh:
            queue_text = fh.read()
        queue_items = _strict_rows(queue_text, "queue", q)
        queue_keys = set()
        for item in queue_items:
            if item.get("status") not in q.STATUSES:
                return False, "queue snapshot has an unknown status"
            if (not str(item.get("company") or "").strip()
                    or not str(item.get("role") or "").strip()
                    or not normalize_url(item.get("link", ""))
                    or q.entry_pr_number(item) < 1):
                return False, "queue snapshot has an incomplete application identity"
            key = q.active_key(item)
            if key in queue_keys:
                return False, "queue snapshot has duplicate application identities"
            queue_keys.add(key)
        if not os.path.isfile(gfile):
            return False, "employer gates snapshot is unavailable"
        with open(gfile, encoding="utf-8") as fh:
            gates_text = fh.read()
        gate_rows = _strict_rows(gates_text, "employer gates", tailor)
        for gate in gate_rows:
            if set(gate) - {"company_aliases", "host", "gate_until", "reason"}:
                return False, "employer gates snapshot has an unknown field"
            aliases = gate.get("company_aliases", [])
            if (not isinstance(aliases, list)
                    or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)):
                return False, "employer gates snapshot has invalid company aliases"
            host = str(gate.get("host") or "").strip().lower()
            if host and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                return False, "employer gates snapshot has an invalid host"
            if not host and not aliases:
                return False, "employer gates snapshot has no matching identity"
            gate_until = str(gate.get("gate_until") or "").strip()
            if gate_until:
                try:
                    datetime.datetime.strptime(gate_until, "%Y-%m-%d")
                except ValueError:
                    return False, "employer gates snapshot has an invalid expiry date"
        old_gate_file = tailor.GATE_FILE
        tailor.GATE_FILE = gfile
        try:
            if tailor.gate_hold({"company": work_item.get("company", ""),
                                 "role": work_item.get("role", ""),
                                 "link": posting_link}):
                return False, "employer application cap is active"
        finally:
            tailor.GATE_FILE = old_gate_file

        company, role = str(work_item.get("company") or "").strip(), str(work_item.get("role") or "").strip()
        if not company or not role:
            return False, "work item company or role is missing"
        candidate = {"company": company, "role": role, "link": posting_link,
                     "pr_number": int(pr["number"])}
        active = q.active_key(candidate)
        unfinished = set(getattr(q, "UNFINISHED", ("queued", "in_progress"))) | {"outcome_unknown"}
        for item in queue_items:
            if item.get("status") in unfinished and q.active_key(item) == active:
                return False, "posting already has an active queue item"

        try:
            tracker_text = _git_output(repo, "show", "origin/main:" + q.TRACKER_PATH)
        except Exception:
            return False, "current applications tracker is unavailable"
        tracker = _strict_rows(tracker_text, "current applications tracker", q)
        tracker_links = set()
        for item in tracker:
            link = normalize_url(item.get("link", ""))
            if (not str(item.get("company") or "").strip()
                    or not str(item.get("role") or "").strip()
                    or not link or not str(item.get("status") or "").strip()):
                return False, "current applications tracker has an incomplete identity"
            if link in tracker_links:
                return False, "current applications tracker has duplicate posting identities"
            tracker_links.add(link)
            if link == posting_link and q.canonical_submitted(item.get("status")):
                return False, "posting already submitted in applications tracker"
        return True, "clear"
    except Exception as exc:
        return False, "preflight failed: " + str(exc)


def open_app_prs():
    """Read all open tailoring PRs so stale and missing reviews are visible."""
    prs = gh_json(["pr", "list", "--repo", GH_REPO, "--state", "open",
                   "--limit", "1000", "--json",
                   "number,title,headRefName,headRefOid,createdAt,body,url"])
    if not isinstance(prs, list):
        raise ValueError("open PR list is malformed")
    if len(prs) >= 1000:
        raise ValueError("open PR list may be truncated")
    if any(not isinstance(pr, dict) for pr in prs):
        raise ValueError("open PR list contains a malformed entry")
    out = [pr for pr in prs
           if str(pr.get("headRefName") or "").startswith("app/")]
    out.sort(key=lambda p: str(p.get("createdAt") or ""))
    return out


def evaluate_readiness(prs, reviewed, skip_prs, skip_links, merged_index,
                       preflight=None, work_items=None):
    """Produce auditable ready/blocked rows without performing any write."""
    preflight = preflight or preflight_candidate
    prepared, numbers, links = [], {}, {}
    for pr in prs:
        try:
            number = int(pr.get("number"))
        except (TypeError, ValueError):
            prepared.append({"pr": pr, "reason": "PR number is invalid"})
            continue
        if number < 1:
            prepared.append({"pr": pr, "reason": "PR number is invalid"})
            continue
        numbers[number] = numbers.get(number, 0) + 1
        try:
            item = (work_items[number] if work_items is not None
                    else load_work_item(pr))
            identity = candidate_links(pr, item)[0]
            links.setdefault(identity, []).append(number)
            prepared.append({"pr": pr, "number": number, "item": item,
                             "link": identity})
        except Exception as exc:
            prepared.append({"pr": pr, "number": number,
                             "reason": "posting identity unavailable: " + str(exc)})

    unavailable_identity = any("link" not in row for row in prepared)
    results = []
    for row in prepared:
        pr = row["pr"]
        number = row.get("number")
        title = str(pr.get("title") or "")
        reason = row.get("reason")
        if number is not None and numbers.get(number, 0) > 1:
            reason = "open PR list has an ambiguous PR identity"
        review_key = str(number) if number is not None else ""
        if not reason and review_key not in reviewed:
            reason = "review is missing"
        if not reason:
            valid, review_reason = approval_result(
                reviewed[review_key], str(pr.get("headRefOid") or ""))
            if not valid:
                reason = review_reason
        if not reason and number in skip_prs:
            reason = "PR number is in skipped.yaml (owner skip)"
        link = row.get("link")
        if not reason and not link:
            reason = "posting identity is unavailable"
        if not reason and link in skip_links:
            reason = "posting link is in skipped.yaml (owner skip)"
        if not reason and link in merged_index:
            reason = "posting link already has a merged PR"
        if not reason and len(links.get(link, [])) > 1:
            reason = "another open PR has the same posting identity"
        if not reason and unavailable_identity:
            reason = "another open app PR has an unavailable posting identity"
        if not reason:
            allowed, detail = preflight(pr, reviewed[review_key], row["item"])
            if not allowed:
                reason = detail
        results.append({"number": number, "title": title,
                        "ready": not bool(reason), "reason": reason or "clear"})
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report readiness without changing GitHub or local state")
    ap.parse_args(argv)
    try:
        reviewed = load_reviewed()
        skip_prs, skip_links = load_skips()
        merged_index = load_merged_pr_index()
        prs = open_app_prs()
    except Exception as exc:
        log("automerge: blocked because an input is unavailable or malformed: " + str(exc))
        return 2

    results = evaluate_readiness(prs, reviewed, skip_prs, skip_links, merged_index)
    if not results:
        print("automerge: no open app/* PRs")
        return 0
    for row in results:
        if row["ready"]:
            print("ready-for-owner-merge #{} {}".format(
                row["number"], row["title"][:70]))
        else:
            print("blocked #{} {}: {}".format(
                row["number"] if row["number"] is not None else "?",
                row["title"][:70], row["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
