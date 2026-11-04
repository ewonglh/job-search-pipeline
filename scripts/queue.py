#!/usr/bin/env python3
"""Application queue for the job-search lanes (stdlib only).

Owner decisions (2026-10-06):

  * a merged ``app/*`` tailoring PR is the **enqueue** event -- one queue item
    per merged tailoring PR; and
  * the merge **is** the per-CV approval. The queue item records the approval
    automatically from the merge (``approved_at`` = the PR's ``mergedAt``,
    ``approved_by`` = the PR's ``mergedBy``). No separate human go-ahead is
    required: merging the variant PR approves *that* CV.

This script is the mechanical half of that flow:

  * ``enqueue`` -- list merged ``app/*`` PRs on your resume repo, keep only
    real tailoring PRs (their diff adds/updates ``variants/**/resume.tex``), and
    append **one** item per PR to the queue store. Strictly idempotent: an item
    is never added twice (dedupe by PR number), a second PR for an application
    that is already active is not queued, and an application already recorded as
    submitted in the tracker is not re-queued. A PR whose branch was later
    deleted is handled: only the PR metadata and the merge commit are read,
    never the (possibly gone) branch.

  * ``list`` / ``next`` -- show the queue / the oldest ``queued`` item.

  * ``status`` -- the drain's bookkeeping: claim (``in_progress``), record a
    submission (``submitted`` + date + exactly what was sent), or ``failed``. A
    ``submitted`` transition refuses an undocumented submission (no submission
    date or no record of what was sent). It does **not** require a separate
    approval: the merge already approved the CV.

**Queue store location.** ``queue.yaml`` lives here in the service dir (default
``./queue.yaml``), next to ``summary_state.json`` and ``gmail_state.json`` --
it is operational runner state, not resume content and not shared bookkeeping.
The drain flips ``queued -> in_progress -> submitted`` several times per
application; committing that churn to the resume repo would add a push per state
change and race the other lanes, and the queue is reconstructible from the merged
PRs plus the tracker. The **canonical** submission record stays
``applications.yaml`` on ``main`` (bookkeeping, per RULES.md); the queue is the
pending-work ledger.

**Fields per item:** ``company``, ``role``, ``source``, ``link`` (posting),
``variant`` (path in the repo), ``pr`` (URL), ``pr_number``, ``enqueued_at``
(UTC; the PR's merge time, since the merge *is* the enqueue event), ``status``
(``queued`` -> ``in_progress`` -> ``submitted`` | ``failed``), ``notes``; plus
``apply_url`` when the tracker carries one, ``approved_at`` / ``approved_by``
(stamped from the merge -- the PR's ``mergedAt`` / ``mergedBy``), and
``submitted_at`` / ``sent`` once the drain records a submission.

Stdlib only; no secrets. The live ``enqueue`` path needs ``gh`` and a resume
clone; ``--prs`` / ``--tracker`` give a fully offline path for tests.

Usage:
  queue.py enqueue [--repo DIR] [--queue FILE] [--dry-run] [--json] [--limit N]
                   [--include-submitted] [--prs FILE] [--tracker FILE]
  queue.py list  [--queue FILE] [--json] [--status STATUS]
  queue.py next  [--queue FILE] [--json]
  queue.py status --pr N --status {in_progress,submitted,failed}
                   [--date D] [--sent TEXT] [--route TEXT] [--notes TEXT]
                   [--queue FILE] [--dry-run] [--json] [--force]

**Tracker write-back.** When ``status --status submitted`` records a submission,
the matching ``applications.yaml`` row is appended on ``origin/main`` (pull,
append, commit, push, all with ``--dry-run``-safe behaviour and a post-push
re-verify). The tracker is the dedup source for tailor.py selection, so a
submission must land there in the same flow it was made. The queue item stores
the exact commit pushed; on the (rare) failure the drain is told to append the
row by hand (the old contract step 10). Failures never block the queue-state
write -- the queue is the operational ledger, the tracker is the canonical
record.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.environ.get("JOBSEARCH_REPO", os.path.join(HERE, "repo"))
QUEUE_FILE = os.environ.get("JOBSEARCH_QUEUE", os.path.join(HERE, "queue.yaml"))
GH_REPO = os.environ.get("JOBSEARCH_GH_REPO", "example-user/resume")

BRANCH_PREFIX = "app/"
TRACKER_PATH = "applications.yaml"
# A tailoring PR adds/updates a variant LaTeX source: either the per-variant
# directory form ``variants/<slug>/resume.tex`` or the flat ``variants/<slug>.tex``.
VARIANT_RE = re.compile(r"^variants/(?:[^/]+/resume|[^/]+)\.tex$")
TITLE_RE = re.compile(r"^app\((?P<slug>[^)]+)\)\s*:\s*(?P<rest>.*)$")

STATUSES = ("queued", "in_progress", "submitted", "failed")
# Statuses that still need work: the queue is drained "one item at a time" and
# ``next`` always returns the oldest of these (never skips past an in-flight
# item to a newer one).
UNFINISHED = ("queued", "in_progress")

# A claim older than this is abandoned, not live. The drain's own budget is
# 3 hours (10800 s) and its worst observed run is 18 minutes, so 4 hours
# cannot collide with a running submission and is short enough that one
# crash costs at most a few hours of stalled queue instead of indefinite.
STALE_CLAIM_SECONDS = 4 * 3600


def claim_is_stale(item, now=None):
    """True when an ``in_progress`` item's claim is abandoned.

    Items claimed before ``claimed_at`` was stamped carry no timestamp. Those
    are treated as stale: the field only exists because every one of them
    did stall and had to be released by hand.
    """
    if str(item.get("status", "")) != "in_progress":
        return False
    stamp = norm_ts(item.get("claimed_at"))
    if not stamp:
        return True
    try:
        claimed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return True
    return ((now or datetime.now(timezone.utc)) - claimed).total_seconds() > STALE_CLAIM_SECONDS

# Allowed forward transitions (a failed item may be retried). The merge of the
# variant PR *is* the per-CV approval, so ``in_progress`` -> ``submitted`` is a
# legal edge; the drain has no separate approval hop.
TRANSITIONS = {
    "queued": {"in_progress", "failed"},
    "in_progress": {"submitted", "failed"},
    "failed": {"queued"},
    "submitted": set(),
}

# Tracker statuses that mean the application has already been submitted.
SUBMITTED_ALIASES = {
    "applied", "submitted", "interview", "interviewing", "interviewed",
    "offer", "offered", "accepted", "rejected", "rejection", "withdrawn",
    "declined",
}

# Source board of the posting (queue item's ``source`` field). Only InternSG
# applications carry the plain-text ``resume.txt`` ATS export; LinkedIn and
# company-ATS applications stay PDF-only.
INTERNSG_SOURCE_RE = re.compile(r"internsg", re.I)

QUEUE_HEADER = (
    "# Application queue -- merged app/* tailoring PRs awaiting submission.\n"
    "# Managed by queue.py (stdlib only); do not hand-edit.\n"
    "# One item per merged tailoring PR. status: queued -> in_progress -> "
    "submitted | failed\n"
    "# Merging the variant PR IS the per-CV approval: approved_at/approved_by\n"
    "# are stamped automatically from the merge (mergedAt / mergedBy). No\n"
    "# separate go-ahead is needed before the drain submits the next item.\n"
    "# The canonical submission record lives in applications.yaml in your\n"
    "# resume repo.\n"
)


# ---------------------------------------------------------------- yaml subset
# Minimal, queue-only YAML reader/writer. Deliberately the same subset accepted
# by the other lanes: a top-level list of flat scalar mappings, plus ``[]``.
def _strip_comment(line):
    out, quote = [], None
    for ch in line:
        if ch in "\"'":
            quote = None if quote == ch else (ch if quote is None else quote)
        if ch == "#" and quote is None:
            break
        out.append(ch)
    return "".join(out)


def _unq(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == "'":
            # undo _quote's '' -> ' doubling (keeps dump/parse idempotent;
            # a value with ' quotes otherwise GROWS on every load/dump cycle)
            return inner.replace("''", "'")
        return inner.replace('\\\"', '"')
    return v


def _scalar(v):
    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "~", ""):
        return None
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    return _unq(v)


def parse_yaml_list(text):
    """Minimal YAML subset: a top-level list of flat mappings."""
    items, cur = [], None
    for raw in text.splitlines():
        s = _strip_comment(raw).strip()
        if not s or s in ("---", "...", "[]"):
            continue
        if s.startswith("- "):
            cur = {}
            items.append(cur)
            s = s[2:].strip()
            if s and ":" in s:
                k, _, v = s.partition(":")
                cur[k.strip()] = _scalar(v)
        elif cur is not None and ":" in s:
            k, _, v = s.partition(":")
            cur[k.strip()] = _scalar(v)
    return items


def _yaml_scalar_out(v):
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if (s == "" or s != s.strip()
            or s[0] in "-?:,[]{}#&*!|>'\"%@`"
            or "#" in s or ": " in s or s[-1] == ":"
            or s.lower() in ("true", "false", "null", "~", "[]")):
        return _quote(s)
    return s


def _quote(s):
    if "'" in s and '"' not in s:
        return '"' + s + '"'
    return "'" + s.replace("'", "''") + "'"


def dump_yaml_list(items):
    lines = []
    for it in items:
        first = True
        for k, v in it.items():
            prefix = "- " if first else "  "
            lines.append(f"{prefix}{k}: {_yaml_scalar_out(v)}")
            first = False
        if first:  # empty mapping
            lines.append("- {}")
    return "\n".join(lines) + "\n"


def dump_queue(items):
    body = dump_yaml_list(items) if items else "[]\n"
    return QUEUE_HEADER + "\n" + body


# ---------------------------------------------------------------- helpers
def slugify(text, maxlen=60):
    text = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:maxlen].rstrip("-") or "item"


def humanize(slug):
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", str(slug or "")) if w)


def norm_text(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def normalize_url(url):
    import urllib.parse
    p = urllib.parse.urlsplit(str(url or "").strip())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def norm_ts(value):
    """Normalise an ISO timestamp to UTC ``YYYY-MM-DDTHH:MM:SSZ`` (ordering-safe)."""
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_submitted(status):
    key = re.sub(r"[\s-]+", "_", norm_text(status).lower())
    return key in SUBMITTED_ALIASES


def is_app_branch(branch):
    return str(branch or "").startswith(BRANCH_PREFIX)


def branch_slug(branch):
    b = str(branch or "")
    return b[len(BRANCH_PREFIX):] if b.startswith(BRANCH_PREFIX) else b


def variant_from_files(files, branch=None):
    """The PR's own ``variants/**/resume.tex`` path, or None.

    Preference order: a path whose slug matches the PR branch (``app/<slug>``),
    then the first matching path. Matching by branch avoids picking up a
    second variant that happens to ride along in the same PR (seen on #82-88,
    where every item initially pointed at the AWS variant).
    """
    paths = []
    for f in files or []:
        path = f.get("path") if isinstance(f, dict) else f
        if path and VARIANT_RE.match(str(path)):
            paths.append(str(path))
    if not paths:
        return None
    if branch:
        m = re.search(r"app/([A-Za-z0-9-]+)", str(branch))
        slug = m.group(1) if m else ""
        if slug:
            def prefix_len(p):
                vslug = p.split("/")[1] if "/" in p else ""
                n = 0
                for a, b in zip(slug, vslug):
                    if a != b:
                        break
                    n += 1
                return n
            best = max(paths, key=prefix_len)
            if prefix_len(best) >= 12:  # distinguishable, not an accident
                return best
    return paths[0]


def normalize_variant(path):
    p = str(path or "").strip().lstrip("./")
    return p


def variant_pdf(path):
    """The built PDF that sits next to a variant's LaTeX source."""
    p = normalize_variant(path)
    return p[:-4] + ".pdf" if p.endswith(".tex") else p


def is_tailoring_pr(files):
    return variant_from_files(files) is not None


def title_parts(pr):
    m = TITLE_RE.match(norm_text(pr.get("title", "")))
    if not m:
        return "", ""
    company = humanize(m.group("slug"))
    rest = norm_text(m.group("rest"))
    role = re.split(r"\s+[—–]\s+|\s+-\s+", rest, maxsplit=1)[-1].strip()
    return company, role


# ---------------------------------------------------------------- queue store
def load_queue(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_yaml_list(fh.read())
    except OSError:
        return []


def write_queue(path, items):
    items = sort_items(items)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dump_queue(items))
    os.replace(tmp, path)


def sort_items(items):
    def key(it):
        return (str(it.get("enqueued_at") or ""), int(it.get("pr_number") or 0))
    return sorted(items, key=key)


def pr_number_of(item):
    n = item.get("pr_number")
    if n in (None, ""):
        m = re.search(r"/pull/(\d+)", str(item.get("pr") or ""))
        return int(m.group(1)) if m else 0
    try:
        return int(n)
    except (TypeError, ValueError):
        return 0


def active_key(item):
    """Identity of an application for the secondary duplicate guard."""
    link = normalize_url(item.get("link", "")).lower()
    if link:
        return "link:" + link
    return "name:" + norm_text(item.get("company", "")).lower() + "|" + \
        norm_text(item.get("role", "")).lower()


def append_note(item, text):
    text = norm_text(text)
    if not text:
        return
    prev = norm_text(item.get("notes", ""))
    item["notes"] = (prev + " | " + text) if prev else text


# ---------------------------------------------------------------- tracker write-back
TRACKER_COMMIT_MSG = ("chore(tracker): record {company} submission "
                      "(PR #{number})")


def _quote_note(s):
    """Quote a tracker note the same way the tracker's own hand-maintained
    format does: single-quoted with ``''`` doubling, and # inside a quoted
    scalar is safe. Long notes are not wrapped (the tracker has no line-width
    rule), so the value stays one line and line-based parsers stay correct.
    """
    return "'" + str(s).replace("'", "''") + "'"


def _tracker_yaml_out(v):
    """Render one scalar for a tracker row, tracker-format aware."""
    s = str(v)
    if s == "":
        return '""'
    if re.fullmatch(r"[+-]?\d+", s):
        return s
    # Date-looking values stay unquoted YAML dates; everything else that could
    # parse as a bool/null or contain ': ' or ' #' gets quoted.
    if ("#" in s or ": " in s or s != s.strip() or s[-1] == ":"
            or s.lower() in ("true", "false", "null", "~")
            or s[0] in "-?:,[]{}#&*!|>'\"%@`"):
        return _quote_note(s)
    return s


def _append_tracker_row(tracker_text, row):
    """Append one ``- key: value`` row to tracker text (list-of-mappings form)."""
    lines = []
    for i, (k, v) in enumerate(row.items()):
        # First key opens the entry ("- key: v"), the rest are 2-space
        # continuations -- exactly the tracker's hand-maintained shape.
        prefix = "- " if i == 0 else "  "
        lines.append(f"{prefix}{k}: {_tracker_yaml_out(v)}")
    body = "\n".join(lines)
    if not tracker_text.strip():
        return body + "\n"
    text = tracker_text.rstrip("\n")
    return text + "\n" + body + "\n"


def build_tracker_row(item, date, sent, route, note=None):
    """The applications.yaml row for one submission (tracker schema per RULES.md)."""
    company = norm_text(item.get("company"))
    role = norm_text(item.get("role"))
    source = norm_text(item.get("source"))
    link = norm_text(item.get("link"))
    variant = normalize_variant(item.get("variant"))
    row = {
        "company": company,
        "role": role,
        "status": "applied",
        "applied_date": date,
        "variant": variant,
        "pr": norm_text(item.get("pr")),
        "pr_number": int(pr_number_of(item)),
    }
    if link:
        row["link"] = link
        row["source"] = source or ("InternSG"
                                   if INTERNSG_SOURCE_RE.search(link)
                                   else "LinkedIn")
    else:
        row["source"] = (source or route or "").strip() or ""
    if route:
        row["route"] = norm_text(route)
    if note is None:
        note = (f"Submitted via drain "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}; "
                f"sent: {norm_text(sent)}")
    row["notes"] = norm_text(note)
    return row


def write_tracker_row(repo, item, date, sent, route, dry_run=False,
                      log=lambda *a: None, note=None):
    """Append the submission row to applications.yaml on origin/main.

    Pull --ff-only, append the row, commit, push, then re-verify by reading
    origin/main. Returns the pushed commit SHA (or '' on failure); failures
    are logged, never raised -- the queue write is not blocked by tracker
    trouble, and the drain falls back to the manual append (README step 10).
    """
    try:
        if not os.path.isdir(os.path.join(repo, ".git")):
            log("queue: tracker write-back: no clone at " + repo)
            return ""
        row = build_tracker_row(item, date, sent, route, note=note)
        if dry_run:
            log("queue: tracker write-back (dry run): would append row for PR #"
                + str(pr_number_of(item)) + " to " + TRACKER_PATH)
            return "dry-run"
        proc = _git(repo, "checkout", "--quiet", "main")
        if proc.returncode != 0:
            log("queue: tracker write-back: checkout main failed: "
                + proc.stderr.strip())
            return ""
        proc = _git(repo, "pull", "--ff-only", "--quiet", "origin", "main")
        if proc.returncode != 0:
            log("queue: tracker write-back: pull failed: "
                + proc.stderr.strip())
            return ""
        tracker_path = os.path.join(repo, TRACKER_PATH)
        with open(tracker_path, encoding="utf-8") as fh:
            tracker_text = fh.read()
        # Idempotency guard: if a row for this PR is already on main (a prior
        # push or a manual append), do not duplicate it.
        if any(pr_number_of(e) == pr_number_of(item) for e in
               parse_yaml_list(tracker_text) if isinstance(e, dict)):
            log("queue: tracker write-back: row for PR #"
                + str(pr_number_of(item)) + " already on main; skipping")
            return "present"
        new_text = _append_tracker_row(tracker_text, row)
        with open(tracker_path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        msg = TRACKER_COMMIT_MSG.format(company=item.get("company", "?"),
                                        number=pr_number_of(item))
        proc = _git(repo, "add", TRACKER_PATH)
        if proc.returncode != 0:
            log("queue: tracker write-back: git add failed: " + proc.stderr.strip())
            return ""
        proc = _git(repo, "commit", "-m", msg)
        if proc.returncode != 0:
            log("queue: tracker write-back: commit failed: " + proc.stderr.strip())
            return ""
        head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        proc = _git(repo, "push", "origin", "main")
        if proc.returncode != 0:
            log("queue: tracker write-back: push failed: " + proc.stderr.strip()
                + " (drain must append the row by hand -- README step 10)")
            return ""
        # Post-push re-verify: origin/main must carry the row.
        _git(repo, "fetch", "--quiet", "--no-tags", "origin", "main")
        verify = _git(repo, "show", f"origin/main:{TRACKER_PATH}")
        ok = False
        if verify.returncode == 0:
            entries = [e for e in parse_yaml_list(verify.stdout)
                       if isinstance(e, dict)]
            ok = any(pr_number_of(e) == pr_number_of(item) for e in entries)
        if not ok:
            log("queue: tracker write-back: post-push verify FAILED for PR #"
                + str(pr_number_of(item)) + " (drain must append by hand)")
            return ""
        log("queue: tracker write-back: recorded PR #"
            + str(pr_number_of(item)) + " on origin/main as " + head)
        return head
    except Exception as exc:  # noqa: BLE001 - never block the queue write
        log(f"queue: tracker write-back: unexpected error: {exc}")
        return ""


# ---------------------------------------------------------------- enqueue: gh
def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def gh_merged_prs(log):
    """Merged PRs on the resume repo, or None when the check is unavailable."""
    try:
        proc = _run(["gh", "pr", "list", "--repo", GH_REPO, "--state", "merged",
                     "--limit", "1000", "--json",
                     "number,title,headRefName,mergedAt,mergeCommit,mergedBy,body,"
                     "url"])
    except Exception as exc:  # noqa: BLE001 - network/subprocess failures expected
        log(f"queue: PR check failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"queue: PR check failed: {proc.stderr.strip() or proc.returncode}")
        return None
    try:
        return json.loads(proc.stdout or "[]")
    except ValueError:
        log("queue: PR check returned invalid JSON")
        return None


def gh_pr_files(number, log):
    """Changed files of one PR (works even after its branch was deleted)."""
    try:
        proc = _run(["gh", "pr", "view", str(number), "--repo", GH_REPO,
                     "--json", "files"])
    except Exception as exc:  # noqa: BLE001
        log(f"queue: file check failed for #{number}: {exc}")
        return []
    if proc.returncode != 0:
        log(f"queue: file check failed for #{number}: "
            f"{proc.stderr.strip() or proc.returncode}")
        return []
    try:
        return json.loads(proc.stdout or "{}").get("files", [])
    except ValueError:
        log(f"queue: file check returned invalid JSON for #{number}")
        return []


def _git(repo, *args, timeout=60):
    return subprocess.run(
        ["git", "-C", repo, "-c", "credential.helper=!gh auth git-credential", *args],
        capture_output=True, text=True, timeout=timeout)


def read_main_tracker(repo, log):
    """Current ``applications.yaml`` on ``origin/main``, or None.

    The **current** tracker is what decides whether an application has already
    been submitted (a submission may have been recorded on ``main`` after the
    tailoring PR merged), so the enqueue guard reads it, not the merge-commit
    state. A deleted PR branch is irrelevant -- ``main`` always has the file.
    """
    if not os.path.isdir(os.path.join(repo, ".git")):
        log(f"queue: no clone at {repo}; cannot read the tracker")
        return None
    try:
        _git(repo, "fetch", "--quiet", "--no-tags", "origin", "main")
        proc = _git(repo, "show", f"origin/main:{TRACKER_PATH}")
    except Exception as exc:  # noqa: BLE001
        log(f"queue: tracker read failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"queue: tracker read failed: {proc.stderr.strip() or proc.returncode}")
        return None
    return proc.stdout


# ---------------------------------------------------------------- enqueue core
def entry_pr_number(entry):
    """PR number recorded on a tracker entry, from ``pr_number`` or its ``pr`` URL."""
    if not isinstance(entry, dict):
        return 0
    try:
        n = int(entry.get("pr_number") or 0)
    except (TypeError, ValueError):
        n = 0
    if n:
        return n
    m = re.search(r"/pull/(\d+)", str(entry.get("pr") or ""))
    return int(m.group(1)) if m else 0


def entry_matches_pr(entry, pr):
    """True when the tracker entry provably belongs to ``pr``.

    A tracker entry is this PR's record only when it carries no PR reference at
    all, or when its PR URL/number is this PR's. Anything else is a different
    application (e.g. an older submission at the same company), so it must not
    make this PR look already handled.
    """
    if not isinstance(entry, dict):
        return False
    e_url = normalize_url(str(entry.get("pr") or "")).lower()
    p_url = normalize_url(str(pr.get("url") or "")).lower()
    if e_url and e_url != p_url:
        return False
    e_num = entry_pr_number(entry)
    return not e_num or e_num == int(pr.get("number") or 0)


def extract_entry(pr, tracker_text):
    """The tracker entry for a PR: by PR URL, then variant path, then company slug."""
    if not tracker_text:
        return None
    entries = [e for e in parse_yaml_list(tracker_text) if isinstance(e, dict)]
    url = normalize_url(pr.get("url", "")).lower()
    if url:
        for e in entries:
            if normalize_url(e.get("pr", "")).lower() == url:
                return e
    vp = normalize_variant(variant_from_files(pr.get("files"), pr.get("headRefName")))
    if vp:
        for e in entries:
            if normalize_variant(e.get("variant")) == vp:
                return e
    slug = branch_slug(pr.get("headRefName", ""))
    if slug:
        for e in entries:
            # Only an entry with no PR of its own may be claimed by company
            # slug. Otherwise a different PR's row (an older application at the
            # same company) is returned and this PR looks already submitted.
            if slugify(e.get("company", "")) == slug \
                    and not str(e.get("pr") or "").strip() \
                    and not entry_pr_number(e):
                return e
    return None


def build_item(pr, entry, variant_path, include_submitted=False):
    e = entry or {}
    t_company, t_role = title_parts(pr)
    company = norm_text(e.get("company")) or t_company or \
        humanize(branch_slug(pr.get("headRefName", "")))
    merged_at = norm_ts(pr.get("mergedAt")) or now_utc()
    merged_by = pr.get("mergedBy")
    if isinstance(merged_by, dict):
        merged_by = merged_by.get("login") or merged_by.get("name") or ""
    link = norm_text(e.get("link"))
    if not link:
        # New workflow: tailor PRs carry the posting link in the body (and no
        # longer edit applications.yaml), so the body is the link fallback.
        m = re.search(r"https?://[^\s)\]<>\"']+", str(pr.get("body") or ""))
        if m:
            link = m.group(0).rstrip(".,;")
    item = {
        "company": company,
        "role": norm_text(e.get("role")) or t_role,
        "source": norm_text(e.get("source")),
        "link": link,
        "variant": normalize_variant(variant_path),
        "pr": norm_text(pr.get("url")),
        "pr_number": int(pr.get("number") or 0),
        "enqueued_at": merged_at,
        "status": "queued",
        # The merge IS the per-CV approval: record it from the PR metadata.
        "approved_at": merged_at,
        "approved_by": norm_text(merged_by),
        "notes": ("merged " + norm_text(pr.get("headRefName"))).strip(),
    }
    if norm_text(e.get("apply_url")):
        item["apply_url"] = norm_text(e.get("apply_url"))
    if include_submitted and canonical_submitted(e.get("status")):
        item["status"] = "submitted"
        if norm_text(e.get("applied_date")):
            item["submitted_at"] = norm_text(e.get("applied_date"))
        append_note(item, "pre-existing submission recorded in tracker")
    return item


# ---------------------------------------------------------------- render
def render_item_line(it):
    return f"{it.get('company', '?')} · {it.get('role', '?')} · {it.get('pr', '')}"


def render_item_block(it):
    lines = [f"pr       {it.get('pr', '')}",
             f"company  {it.get('company', '')}",
             f"role     {it.get('role', '')}",
             f"source   {it.get('source', '')}",
             f"link     {it.get('link', '')}"]
    if it.get("apply_url"):
        lines.append(f"apply    {it['apply_url']}")
    lines += [f"variant  {it.get('variant', '')}",
              f"pdf      {variant_pdf(it.get('variant'))}",
              f"status   {it.get('status', '')}",
              f"enqueued {it.get('enqueued_at', '')}"]
    if it.get("approved_at"):
        by = norm_text(it.get("approved_by"))
        lines.append(f"approved {it.get('approved_at', '')} (merge approval"
                     + (f" by {by}" if by else "") + ")")
    else:
        lines.append("approved (none recorded)")
    if it.get("submitted_at"):
        lines.append(f"submitted {it.get('submitted_at', '')}")
    if it.get("sent"):
        lines.append(f"sent     {it.get('sent', '')}")
    if it.get("notes"):
        lines.append(f"notes    {it.get('notes', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli actions
def cmd_enqueue(args, log):
    items = load_queue(args.queue)
    if args.prs:
        try:
            with open(args.prs, encoding="utf-8") as fh:
                prs = json.load(fh)
        except (OSError, ValueError) as exc:
            log(f"queue: cannot read --prs {args.prs}: {exc}")
            return 1
    else:
        prs = gh_merged_prs(log)
        if prs is None:
            return 1

    tracker_text = None
    if args.tracker:
        try:
            with open(args.tracker, encoding="utf-8") as fh:
                tracker_text = fh.read()
        except OSError as exc:
            log(f"queue: cannot read --tracker {args.tracker}: {exc}")
            return 1
    else:
        # The current tracker on origin/main (not the merge-commit state).
        tracker_text = read_main_tracker(args.repo, log)

    # Attach changed-file lists (live: one API call per unseen app/* candidate).
    # A deleted branch is never consulted -- only the PR metadata and diff.
    seen_numbers = {pr_number_of(it) for it in items}
    prs2 = []
    for pr in prs:
        pr = dict(pr)
        if pr.get("files") is None:
            number = int(pr.get("number") or 0)
            if is_app_branch(pr.get("headRefName", "")) and number not in seen_numbers:
                pr["files"] = gh_pr_files(number, log)
            else:
                pr["files"] = []
        pr["_tracker"] = tracker_text
        prs2.append(pr)

    merged_items, added, skipped = enqueue(
        items, prs2, args.include_submitted, args.limit)
    for number, reason in skipped:
        log(f"queue: skip #{number}: {reason}")
    for it in added:
        log(f"queue: queued #{it['pr_number']} {it['company']} · {it['role']}")

    if not args.dry_run:
        write_queue(args.queue, merged_items)

    if args.json:
        print(json.dumps({"added": added, "skipped": [
            {"pr_number": n, "reason": r} for n, r in skipped]},
            indent=2, ensure_ascii=False))
    else:
        for it in added:
            print(f"queued: {render_item_line(it)}")
    return 0


def enqueue(existing, prs_with_trackers, include_submitted=False, limit=0):
    """Pure merge: returns ``(items, added, skipped)``.

    Each PR mapping carries ``files`` (its changed paths) and ``_tracker`` (the
    ``applications.yaml`` text at its merge commit). Idempotent by PR number.
    """
    items = [dict(it) for it in existing]
    seen = {pr_number_of(it) for it in items}
    seen.discard(0)
    active = {active_key(it) for it in items
              if it.get("status") in UNFINISHED}
    added, skipped = [], []
    for pr in sorted(prs_with_trackers, key=lambda p: int(p.get("number") or 0)):
        number = int(pr.get("number") or 0)
        branch = pr.get("headRefName", "")
        if not is_app_branch(branch):
            skipped.append((number, "not an app/* PR"))
            continue
        if number in seen:
            skipped.append((number, "already queued"))
            continue
        vp = variant_from_files(pr.get("files"), pr.get("headRefName"))
        if not vp:
            skipped.append((number, "not a tailoring PR (no variants/**/resume.tex)"))
            continue
        entry = extract_entry(pr, pr.get("_tracker"))
        if entry is not None and canonical_submitted(entry.get("status")) \
                and not include_submitted and entry_matches_pr(entry, pr):
            skipped.append((number, "already submitted in tracker"))
            continue
        item = build_item(pr, entry, vp, include_submitted=include_submitted)
        if item["status"] == "queued" and active_key(item) in active:
            skipped.append((number, "already active for this application"))
            continue
        if limit and len(added) >= limit:
            skipped.append((number, "limit reached"))
            continue
        seen.add(number)
        if item["status"] == "queued":
            active.add(active_key(item))
        added.append(item)
        items.append(item)
    return sort_items(items), added, skipped


def cmd_list(args, log):
    items = sort_items(load_queue(args.queue))
    if args.status:
        items = [it for it in items if it.get("status") == args.status]
    if args.json:
        # ``claim_stale`` lets a caller (the drain trigger) tell a live claim
        # from an abandoned one without duplicating the policy.
        view = []
        for it in items:
            v = dict(it)
            v["claim_stale"] = claim_is_stale(it)
            view.append(v)
        print(json.dumps(view, indent=2, ensure_ascii=False))
    else:
        for it in items:
            flag = " (stale claim)" if claim_is_stale(it) else ""
            print(f"{it.get('status', '?'):11s} {render_item_line(it)}{flag}")
    return 0


def cmd_release_stale(args, log):
    """Return abandoned in_progress claims to queued.

    Recovery for a drain that died between claiming and recording an outcome.
    The claim itself is the only evidence that work was in flight, and a
    stranded claim blocks the drain trigger indefinitely, so releasing it is
    always safe: nothing was recorded, so nothing is lost by retrying.
    """
    items = load_queue(args.queue)
    released = []
    for i, it in enumerate(items):
        if not claim_is_stale(it):
            continue
        row = dict(it)
        row["status"] = "queued"
        append_note(row, "released stale in_progress claim"
                         + (" held since " + str(it.get("claimed_at"))
                            if it.get("claimed_at") else " (no claim timestamp)")
                         + "; no outcome was recorded, so the item is retried")
        items[i] = row
        released.append(row)
    if not released:
        log("queue: no stale claims")
        return 0
    if not args.dry_run:
        write_queue(args.queue, items)
    for row in released:
        verb = "would release" if args.dry_run else "released"
        log(f"queue: {verb} #{pr_number_of(row)} {row.get('company', '')}")
    return 0


def present_item(it):
    """An output-only view adding the CV a reviewer must see before sendoff."""
    view = dict(it)
    view["pdf"] = variant_pdf(it.get("variant"))
    return view


def cmd_next(args, log):
    """The current item: oldest unfinished (queued/in_progress).

    Never skips past an in-flight item to a newer one -- the drain works the
    queue one item at a time. Prints the CV to submit (variant path, built PDF,
    PR link) plus the merge approval stamped at enqueue.
    """
    items = [it for it in sort_items(load_queue(args.queue))
             if it.get("status") in UNFINISHED]
    if not items:
        return 0
    it = items[0]
    if args.json:
        print(json.dumps(present_item(it), indent=2, ensure_ascii=False))
    else:
        print(render_item_block(it))
    return 0


def cmd_status(args, log):
    items = load_queue(args.queue)
    idx = next((i for i, it in enumerate(items)
                if pr_number_of(it) == args.pr), None)
    if idx is None:
        log(f"queue: no item with pr #{args.pr}")
        return 1
    it = dict(items[idx])
    cur, new = it.get("status", "queued"), args.status
    if new not in STATUSES:
        log(f"queue: invalid status {new!r} (one of {', '.join(STATUSES)})")
        return 2
    if new == "submitted":
        if not args.date or not args.sent:
            log("queue: recording a submission requires --date YYYY-MM-DD and "
                "--sent '<exactly what was sent>'")
            return 2
    if new == "failed" and not args.notes:
        log("queue: recording a failure requires --notes '<reason>'")
        return 2
    if new != cur and new not in TRANSITIONS.get(cur, set()) and not args.force:
        allowed = ", ".join(sorted(TRANSITIONS.get(cur, set()))) or "(terminal)"
        log(f"queue: cannot move #{args.pr} {cur} -> {new}; allowed: {allowed} "
            f"(use --force to override)")
        return 2

    it["status"] = new
    if new == "in_progress":
        # Stamp the claim so a later run can tell a live claim from an
        # abandoned one. Without it a claim orphaned by a crashed or timed-out
        # drain blocks the trigger forever (measured 2026-10-29 and twice on
        # 2026-10-31: 3 orphaned claims, each costing a full queue stall).
        it["claimed_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    if new == "submitted":
        it["submitted_at"] = args.date
        it["sent"] = norm_text(args.sent)
    if args.route:
        append_note(it, "route: " + args.route)
    if args.notes:
        append_note(it, args.notes)

    # Tracker write-back (canonical record, closes the tailor-dedup gap):
    # a recorded submission must land in applications.yaml on origin/main in
    # the same flow. The queue write below is NOT gated on this succeeding.
    tracker_sha = ""
    if new == "submitted" and not args.dry_run and not args.no_tracker:
        tracker_sha = write_tracker_row(args.repo, it, args.date, args.sent,
                                        args.route, dry_run=False, log=log)
        if tracker_sha and tracker_sha != "present":
            append_note(it, "tracker row committed " + tracker_sha)
        elif tracker_sha == "present":
            log("queue: tracker row for PR #" + str(args.pr)
                + " already on main; nothing to do")
        elif not args.json:
            log("queue: NOTE tracker write-back did not verify for PR #"
                + str(args.pr) + " -- append the applications.yaml row by hand "
                "(README step 10) before calling this recorded")

    items[idx] = it
    if not args.dry_run:
        write_queue(args.queue, items)

    log(f"queue: #{args.pr} -> {new}")
    if args.json:
        print(json.dumps(it, indent=2, ensure_ascii=False))
    else:
        print(f"{new}: {render_item_line(it)}")
    return 0


# ---------------------------------------------------------------- parser
def add_common(ap):
    ap.add_argument("--queue", default=QUEUE_FILE,
                    help="queue store (default: ./queue.yaml, env JOBSEARCH_QUEUE)")


def build_parser():
    ap = argparse.ArgumentParser(
        description="Application queue: merged tailoring PRs -> drain contract.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    e = sub.add_parser("enqueue", help="append newly merged app/* tailoring PRs")
    add_common(e)
    e.add_argument("--repo", default=REPO_DIR,
                   help="resume clone (default: ./repo, env JOBSEARCH_REPO)")
    e.add_argument("--dry-run", action="store_true", help="print, write nothing")
    e.add_argument("--json", action="store_true", help="emit added/skipped as JSON")
    e.add_argument("--limit", type=int, default=0, help="add at most N items (0 = all)")
    e.add_argument("--include-submitted", action="store_true",
                   help="also enqueue apps already recorded as submitted")
    e.add_argument("--prs", help="offline: JSON list of PRs (gh shape)")
    e.add_argument("--tracker", help="offline: applications.yaml text for candidates")

    l = sub.add_parser("list", help="list queue items (oldest first)")
    add_common(l)
    l.add_argument("--json", action="store_true")
    l.add_argument("--status", choices=STATUSES, help="filter by status")

    n = sub.add_parser("next", help="print the oldest queued item")
    add_common(n)
    n.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="update an item (drain bookkeeping)")
    add_common(s)
    s.add_argument("--repo", default=REPO_DIR,
                   help="resume clone for the tracker write-back "
                        "(default: ./repo, env JOBSEARCH_REPO)")
    s.add_argument("--pr", type=int, required=True, help="PR number of the item")
    s.add_argument("--status", required=True,
                   choices=["queued", "in_progress", "submitted", "failed"],
                   help="new status (queued re-opens a failed item)")
    s.add_argument("--date", help="submission date (YYYY-MM-DD); required for submitted")
    s.add_argument("--sent", help="exactly what was sent, required for submitted")
    s.add_argument("--route", help="submission route (site/ATS or LinkedIn Easy Apply)")
    s.add_argument("--notes", help="free-form note (required for failed)")
    s.add_argument("--no-tracker", action="store_true",
                   help="skip the automatic applications.yaml write-back "
                        "(e.g. when the row was appended by hand)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--force", action="store_true", help="override transition rules")

    r = sub.add_parser("release-stale",
                       help="return abandoned in_progress claims to queued")
    add_common(r)
    r.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)  # noqa: E731
    if args.cmd == "enqueue":
        return cmd_enqueue(args, log)
    if args.cmd == "list":
        return cmd_list(args, log)
    if args.cmd == "next":
        return cmd_next(args, log)
    if args.cmd == "status":
        return cmd_status(args, log)
    if args.cmd == "release-stale":
        return cmd_release_stale(args, log)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
