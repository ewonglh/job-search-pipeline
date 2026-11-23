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

  * ``claim`` -- verify a unique current posting, exact selected profile, fresh
    guarded JD, owner skips, and caps, then durably persist a unique attempt ID.
    External submission needs this durable claim and a verified material snapshot.
  * ``prepare-snapshot`` / ``verify-snapshot`` -- retain exact PDF and answer
    bytes for this application/attempt, then check retained and current bytes.
  * ``status`` -- record a confirmed submission against that attempt ID before
    snapshot finalization and tracker I/O, or mark an item failed before an attempt.
  * ``finalize-snapshot`` -- repair provenance for an already submitted attempt;
    it never submits or reconstructs missing historical files.
  * ``release-stale`` / ``reconcile`` -- keep interrupted attempts in
    ``outcome_unknown`` until explicit evidence establishes the outcome.

**Queue store location.** The queue is operational runner state, not resume
content or shared bookkeeping. Its default is
``~/.local/state/job-search-pipeline/queue.yaml``; override it with
``JOBSEARCH_QUEUE``. Keep it outside the code checkout.
Owner-authorized merge observations and their pending or terminal outcomes are
stored in the adjacent private ``queue.yaml.merge-intake.json`` sidecar. A
pending record is replayed after restart until queue insertion or a verified
exclusion is durable.
The drain flips ``queued -> in_progress -> submitted`` several times per
application; committing that churn to the resume repo would add a push per state
change and race the other lanes, and the queue is reconstructible from the merged
PRs plus the tracker. The **canonical** submission record stays
``applications.yaml`` on ``main`` (bookkeeping, per RULES.md); the queue is the
pending-work ledger.

Snapshot blobs and manifests default to
``~/.local/state/job-search-pipeline/submission-snapshots`` or
``JOBSEARCH_SNAPSHOT_ROOT``. This private store must remain outside Git
checkouts. The queue stores content IDs and application/attempt bindings only.

**Fields per item:** ``company``, ``role``, ``source``, ``link`` (posting),
``variant`` (path in the repo), ``pr`` (URL), ``pr_number``, ``enqueued_at``
(UTC; the PR's merge time), ``status``, and ``notes``; plus ``apply_url`` when
available, ``approved_at`` / ``approved_by`` from the merge, durable
``attempt_id`` / ``attempt_state`` / ``submit_gate`` on claim, and
``submitted_at`` / ``sent`` / ``confirmation_evidence`` after confirmation.

Stdlib only; no secrets. The live ``enqueue`` path needs ``gh`` and a resume
clone. It paginates the complete merged-PR history and checkpoints owner
merges before file and posting checks. ``--prs`` / ``--tracker`` provide
read-only offline previews only with ``--dry-run``.

Usage:
  queue.py enqueue [--repo DIR] [--queue FILE] [--dry-run] [--json] [--limit N]
                   [--include-submitted] [--prs FILE] [--tracker FILE]
  queue.py list  [--queue FILE] [--json] [--status STATUS]
  queue.py next  [--queue FILE] [--json]
  queue.py claim --pr N [--repo DIR] [--profile FILE] [--queue FILE] [--json]
  queue.py prepare-snapshot --pr N --attempt-id ID --pdf FILE --answers FILE
                            --operator-verification TEXT
  queue.py verify-snapshot --pr N --attempt-id ID --pdf FILE --answers FILE
  queue.py status --pr N --status submitted --attempt-id ID --date D --sent TEXT
                   --confirmation-evidence TEXT [--route TEXT] [--queue FILE]
  queue.py finalize-snapshot --pr N --attempt-id ID
  queue.py release-stale [--queue FILE]
  queue.py reconcile --pr N --attempt-id ID --result {retry,submitted}
                     --evidence-source TEXT --evidence-observation TEXT
  queue.py sync-tracker --pr N [--repo DIR] [--queue FILE]

**Tracker write-back.** A confirmed submission and ``tracker_sync: pending``
are durably stored before tracker I/O. The tracker row is then appended and
verified on ``origin/main``. Failure leaves the queue item ``submitted`` with
``tracker_sync: error``; ``sync-tracker`` repairs bookkeeping without
reopening the attempt or touching a form.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from config import (default_repo_dir, default_runtime_path, github_repo_setting,
                    path_setting)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = path_setting("JOBSEARCH_REPO", default_repo_dir())
QUEUE_FILE = path_setting("JOBSEARCH_QUEUE", default_runtime_path("queue.yaml"))
SNAPSHOT_ROOT = path_setting("JOBSEARCH_SNAPSHOT_ROOT",
                             default_runtime_path("submission-snapshots"))
GH_REPO = github_repo_setting("JOBSEARCH_GH_REPO", "your-user/your-resume-repo")

BRANCH_PREFIX = "app/"
TRACKER_PATH = "applications.yaml"
# A tailoring PR adds/updates a variant LaTeX source: either the per-variant
# directory form ``variants/<slug>/resume.tex`` or the flat ``variants/<slug>.tex``.
VARIANT_RE = re.compile(r"^variants/(?:[^/]+/resume|[^/]+)\.tex$")
TITLE_RE = re.compile(r"^app\((?P<slug>[^)]+)\)\s*:\s*(?P<rest>.*)$")

STATUSES = ("queued", "in_progress", "submitted", "failed",
            "outcome_unknown", "skipped")
# Statuses that still need work: the queue is drained "one item at a time" and
# ``next`` always returns the oldest of these (never skips past an in-flight
# item to a newer one).
UNFINISHED = ("queued", "in_progress")
APPLICATION_GUARD_STATUSES = ("queued", "in_progress", "submitted",
                              "outcome_unknown")

# A claim older than this is abandoned, not live. The drain's own budget is
# 3 hours (10800 s) and its worst observed run is 18 minutes, so 4 hours
# cannot collide with a running submission and is short enough that one
# crash costs at most a few hours of stalled queue instead of indefinite.
STALE_CLAIM_SECONDS = 4 * 3600


def claim_is_stale(item, now=None):
    """True when a claim is old or lacks its durable submit-gate marker."""
    if str(item.get("status", "")) != "in_progress":
        return False
    if (not item.get("attempt_id") or item.get("attempt_state") != "authorized"
            or item.get("submit_gate") != "durable"):
        return True
    stamp = norm_ts(item.get("claimed_at"))
    if not stamp:
        return True
    try:
        claimed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    if claimed > current + timedelta(minutes=5):
        return True
    return (current - claimed).total_seconds() > STALE_CLAIM_SECONDS


def snapshot_verification_is_fresh(item, now=None):
    """A pre-submit material check expires after two minutes."""
    stamp = norm_ts(item.get("snapshot_verified_at"))
    if not stamp:
        return False
    try:
        checked = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    age = (current - checked).total_seconds()
    return -30 <= age <= 120

# Status changes never create or reopen an external-submit attempt. A failed
# item is terminal; claimed attempts are retried only through reconciliation.
TRANSITIONS = {
    "queued": {"failed"},
    "in_progress": {"submitted"},
    "failed": set(),
    "submitted": set(),
    "outcome_unknown": set(),
    "skipped": set(),
}


class QueueStateError(ValueError):
    """The queue cannot be read or safely updated."""

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
    "# One item per merged tailoring PR. Attempts are durable and never auto-retried.\n"
    "# status: queued -> in_progress -> submitted | outcome_unknown | failed\n"
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


def parse_queue(text, status_values=STATUSES):
    """Parse the queue's flat YAML subset and reject anything ambiguous."""
    rows, current = [], None
    empty_list_seen = False
    for line_no, raw in enumerate(str(text).splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue
        if stripped == "[]":
            if rows or current is not None or empty_list_seen:
                raise QueueStateError("malformed queue at line %d" % line_no)
            empty_list_seen = True
            continue
        if stripped.startswith("- "):
            if empty_list_seen:
                raise QueueStateError("malformed queue at line %d" % line_no)
            current = {}
            rows.append(current)
            entry = stripped[2:].strip()
            if entry == "{}":
                continue
            if not entry or ":" not in entry:
                raise QueueStateError("malformed queue at line %d" % line_no)
        elif current is not None and raw.startswith("  ") and not raw.startswith("   "):
            entry = stripped
            if ":" not in entry:
                raise QueueStateError("malformed queue at line %d" % line_no)
        else:
            raise QueueStateError("malformed queue at line %d" % line_no)
        key, _, value = entry.partition(":")
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) or key in current:
            raise QueueStateError("malformed queue key at line %d" % line_no)
        quoted = (len(value) >= 2 and value[0] == value[-1]
                  and value[0] in "\"'")
        if (not quoted and (value.startswith(("[", "{"))
                            or ": " in value)):
            raise QueueStateError("unsupported queue value at line %d" % line_no)
        if value.startswith(("\"", "'")) and not quoted:
            raise QueueStateError("malformed quoted value at line %d" % line_no)
        current[key] = _scalar(value)
    if empty_list_seen:
        return []
    if not rows and status_values is not None:
        raise QueueStateError("queue has no explicit empty-list marker or entries")
    for row in rows:
        if not isinstance(row, dict):
            raise QueueStateError("queue entries must be mappings")
        number = row.get("pr_number")
        if number not in (None, ""):
            try:
                if int(number) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise QueueStateError("queue entry has an invalid pr_number")
        status = row.get("status", "queued")
        if status_values is not None and status not in status_values:
            raise QueueStateError("queue entry has an unknown status")
    return rows


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
@contextmanager
def queue_lock(path):
    """Serialize queue mutations across processes on Windows and POSIX."""
    lock_path = os.path.abspath(path) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            deadline = time.monotonic() + 120
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise QueueStateError("timed out waiting for queue lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_queue(path):
    try:
        with open(path, encoding="utf-8") as fh:
            items = parse_queue(fh.read())
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise QueueStateError("cannot read queue: %s" % type(exc).__name__)
    seen = set()
    for item in items:
        number = pr_number_of(item)
        if not number or number in seen:
            raise QueueStateError("queue has a missing or duplicate PR identity")
        seen.add(number)
    return items


def _write_queue_unlocked(path, items):
    items = sort_items(items)
    serialized = dump_queue(items)
    parsed = parse_queue(serialized)
    seen = set()
    for item in parsed:
        number = pr_number_of(item)
        if not number or number in seen:
            raise QueueStateError("refusing to write a missing or duplicate PR identity")
        seen.add(number)
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                                     suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            move_file = kernel.MoveFileExW
            move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p,
                                  ctypes.c_uint32)
            move_file.restype = ctypes.c_int
            flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            if not move_file(temporary, absolute, flags):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, absolute)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_queue(path, items):
    """Atomically write a complete queue while holding its process lock."""
    with queue_lock(path):
        _write_queue_unlocked(path, items)


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
TRACKER_CONFLICT = "identity-conflict"


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
        import application_identity as identity
        try:
            tracker_rows = parse_queue(tracker_text, status_values=None)
            target_id = _canonical_queue_identity(item)
            target_pr = identity.pr_number(item)
            resolution = identity.resolve_applications(tracker_rows, [item])
        except (QueueStateError, TypeError, ValueError) as exc:
            log("queue: tracker write-back: application identity is ambiguous: " + str(exc))
            return TRACKER_CONFLICT
        if any("queue:1" in conflict.get("row_refs", [])
               for conflict in resolution.get("conflicts", [])):
            log("queue: tracker write-back: tracker identity conflict for PR #"
                + str(pr_number_of(item)))
            return TRACKER_CONFLICT
        same_pr = []
        for entry in tracker_rows:
            try:
                entry_pr = identity.pr_number(entry)
            except (TypeError, ValueError):
                log("queue: tracker write-back: tracker row has contradictory PR aliases")
                return TRACKER_CONFLICT
            entry_link = identity.normalize_posting_url(entry.get("link"))
            if entry_pr == target_pr:
                same_pr.append(entry)
                if entry_link and entry_link != target_id[5:]:
                    log("queue: tracker write-back: PR points at a different posting")
                    return TRACKER_CONFLICT
            elif entry_link and entry_link == target_id[5:]:
                log("queue: tracker write-back: posting is already attached to another PR")
                return TRACKER_CONFLICT
        if len(same_pr) > 1:
            log("queue: tracker write-back: duplicate tracker rows for PR #"
                + str(target_pr))
            return TRACKER_CONFLICT
        if same_pr:
            existing = same_pr[0]
            for key in ("company", "role"):
                old, new = norm_text(existing.get(key)), norm_text(item.get(key))
                if old and new and old.casefold() != new.casefold():
                    log("queue: tracker write-back: PR row has conflicting %s" % key)
                    return TRACKER_CONFLICT
            log("queue: tracker write-back: row for PR #"
                + str(target_pr) + " already on main; skipping")
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


def gh_merged_prs(log, on_page=None):
    """Return every merged PR by explicit GraphQL cursor pagination.

    ``on_page`` is called as soon as a page is decoded. Live intake uses it to
    persist owner-authorized merges before requesting another page or looking
    up any changed files.
    """
    return _gh_merged_prs_paginated(log, on_page=on_page)


MERGED_PR_QUERY = (
    "query($owner:String!, $name:String!, $endCursor:String) { "
    "repository(owner:$owner, name:$name) { pullRequests( "
    "first:100, after:$endCursor, states:MERGED, "
    "orderBy:{field:CREATED_AT, direction:DESC}) { nodes { number title "
    "headRefName headRefOid mergedAt mergeCommit { oid } mergedBy { login } "
    "body url } pageInfo { hasNextPage endCursor } } } }"
)


def _gh_merged_prs_paginated(log, on_page=None):
    owner, name = GH_REPO.split("/", 1)
    cursor = None
    cursors = set()
    all_prs = []
    while True:
        cmd = ["gh", "api", "graphql", "-f", "query=" + MERGED_PR_QUERY,
               "-F", "owner=" + owner, "-F", "name=" + name]
        if cursor:
            cmd += ["-F", "endCursor=" + cursor]
        try:
            proc = _run(cmd, timeout=120)
        except Exception as exc:  # noqa: BLE001 - network/subprocess failures expected
            log(f"queue: PR check failed: {exc}")
            return None
        if proc.returncode != 0:
            log(f"queue: PR check failed: {proc.stderr.strip() or proc.returncode}")
            return None
        try:
            response = json.loads(proc.stdout or "{}")
            if response.get("errors"):
                raise ValueError("GraphQL returned errors")
            connection = response["data"]["repository"]["pullRequests"]
            nodes = connection["nodes"]
            page = connection["pageInfo"]
            if (not isinstance(nodes, list) or any(not isinstance(row, dict) for row in nodes)
                    or not isinstance(page, dict)
                    or not isinstance(page.get("hasNextPage"), bool)):
                raise ValueError("GraphQL page is malformed")
            next_cursor = page.get("endCursor")
            if page["hasNextPage"] and (not isinstance(next_cursor, str)
                                         or not next_cursor or next_cursor in cursors):
                raise ValueError("GraphQL pagination cursor is missing or repeated")
        except (KeyError, TypeError, ValueError) as exc:
            log(f"queue: merged PR response is malformed: {exc}")
            return None
        if on_page is not None:
            on_page(nodes)
        all_prs.extend(nodes)
        if not page["hasNextPage"]:
            return all_prs
        cursors.add(next_cursor)
        cursor = next_cursor


def gh_pr_files(number, log):
    """All changed files of one PR, or None when file metadata is unavailable."""
    path = f"repos/{GH_REPO}/pulls/{int(number)}/files"
    try:
        proc = _run(["gh", "api", "--paginate", "--slurp", path,
                     "-F", "per_page=100"], timeout=120)
    except Exception as exc:  # noqa: BLE001
        log(f"queue: file check failed for #{number}: {exc}")
        return None
    if proc.returncode != 0:
        log(f"queue: file check failed for #{number}: "
            f"{proc.stderr.strip() or proc.returncode}")
        return None
    try:
        pages = json.loads(proc.stdout or "[]")
        if not isinstance(pages, list):
            raise ValueError("expected page list")
        if not pages:
            return []
        if all(isinstance(page, list) for page in pages):
            files = [item for page in pages for item in page]
        elif all(isinstance(item, dict) for item in pages):
            files = pages
        else:
            raise ValueError("malformed file page")
        if any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
               or not item.get("path") for item in files):
            raise ValueError("malformed file record")
        return [{"path": item["path"]} for item in files]
    except (TypeError, ValueError):
        log(f"queue: file check returned invalid JSON for #{number}")
        return None


INTAKE_SCHEMA = 1
INTAKE_STATES = {"pending", "admitted", "excluded"}


def merge_intake_path(queue_path):
    """Private durable merge outcomes beside the configured queue ledger."""
    return os.path.abspath(os.fspath(queue_path)) + ".merge-intake.json"


def _empty_merge_intake():
    return {"schema": INTAKE_SCHEMA, "updated_at": now_utc(), "records": {}}


def load_merge_intake(queue_path):
    path = merge_intake_path(queue_path)
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return _empty_merge_intake()
    except (OSError, UnicodeError, ValueError) as exc:
        raise QueueStateError("cannot read merge intake state") from exc
    if (not isinstance(state, dict) or state.get("schema") != INTAKE_SCHEMA
            or not isinstance(state.get("records"), dict)):
        raise QueueStateError("merge intake state is malformed or unsupported")
    for key, record in state["records"].items():
        if (not isinstance(key, str) or not key.isdigit()
                or not isinstance(record, dict)
                or int(record.get("pr_number") or 0) != int(key)
                or record.get("state") not in INTAKE_STATES
                or not isinstance(record.get("identity"), dict)
                or not isinstance(record.get("head"), dict)):
            raise QueueStateError("merge intake state contains an invalid record")
    return state


def _write_merge_intake_unlocked(queue_path, state):
    absolute = merge_intake_path(queue_path)
    directory = os.path.dirname(absolute)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(absolute) + ".",
                                     suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            move_file = kernel.MoveFileExW
            move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p,
                                  ctypes.c_uint32)
            move_file.restype = ctypes.c_int
            flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            if not move_file(temporary, absolute, flags):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, absolute)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _posting_url_from_body(body):
    for match in re.finditer(r"https?://[^\s)\]<>\"']+", str(body or "")):
        link = match.group(0).rstrip(".,;")
        if "github.com" not in link.casefold():
            return link
    return ""


def _observed_merge_record(pr):
    try:
        number = int(pr.get("number") or 0)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return None
    merged_by = pr.get("mergedBy")
    if not isinstance(merged_by, dict):
        return None
    login = norm_text(merged_by.get("login"))
    owner = GH_REPO.split("/", 1)[0]
    if not login or login.casefold() != owner.casefold():
        return None
    branch = norm_text(pr.get("headRefName"))
    merge_commit = pr.get("mergeCommit")
    merge_sha = (norm_text(merge_commit.get("oid"))
                 if isinstance(merge_commit, dict) else "")
    url = norm_text(pr.get("url"))
    merged_at = norm_ts(pr.get("mergedAt"))
    posting_url = _posting_url_from_body(pr.get("body"))
    stored_pr = {
        "number": number,
        "title": norm_text(pr.get("title")),
        "headRefName": branch,
        "headRefOid": norm_text(pr.get("headRefOid")),
        "mergedAt": merged_at,
        "mergeCommit": {"oid": merge_sha} if merge_sha else None,
        "mergedBy": {"login": login},
        "url": url,
        "posting_url": posting_url,
    }
    reason = "merge observed; intake processing is pending"
    return {
        "pr_number": number,
        "state": "pending",
        "reason_code": "metadata_unavailable",
        "reason": reason,
        "identity": {"pr_number": number, "pr_url": url,
                     "posting_url": posting_url, "merged_at": merged_at,
                     "merged_by": login, "merge_commit": merge_sha},
        "head": {"branch": branch, "sha": stored_pr["headRefOid"]},
        "pr": stored_pr,
        "observed_at": now_utc(),
        "updated_at": now_utc(),
    }


def _record_observed_page(queue_path, prs):
    """Checkpoint owner merges for one API page before any per-PR lookups."""
    if not any(_observed_merge_record(pr) is not None for pr in prs):
        return
    with queue_lock(queue_path):
        state = load_merge_intake(queue_path)
        records = state["records"]
        changed = False
        for pr in prs:
            record = _observed_merge_record(pr)
            if record is None:
                continue
            key = str(record["pr_number"])
            old = records.get(key)
            if old and old["state"] != "pending":
                continue
            if old:
                if old.get("identity", {}).get("pr_url") not in ("", record["identity"]["pr_url"]):
                    raise QueueStateError("merged PR identity changed after observation")
                # Keep a successful file lookup across restart while refreshing
                # authoritative merge/head metadata from the source page.
                for field in ("files", "files_observed_at"):
                    if field in old:
                        record[field] = old[field]
                record["observed_at"] = old.get("observed_at") or record["observed_at"]
                if (old.get("pr") == record.get("pr")
                        and old.get("identity") == record.get("identity")
                        and old.get("head") == record.get("head")):
                    continue
            if not record["head"].get("branch"):
                record["reason"] = "owner merge branch metadata is unavailable"
            elif not is_app_branch(record["head"].get("branch")):
                record.update({"state": "excluded", "reason_code": "not_application_pr",
                               "reason": "merged PR is outside the app/ branch"})
            elif not record["pr"].get("mergedAt"):
                record["reason"] = "owner merge timestamp is unavailable"
            records[key] = record
            changed = True
        if changed:
            state["updated_at"] = now_utc()
            _write_merge_intake_unlocked(queue_path, state)


def _set_intake_outcome(queue_path, number, state_name, reason_code, reason,
                        **fields):
    if state_name not in INTAKE_STATES:
        raise QueueStateError("invalid merge intake outcome")
    with queue_lock(queue_path):
        state = load_merge_intake(queue_path)
        record = state["records"].get(str(int(number)))
        if record is None:
            raise QueueStateError("merge intake record disappeared")
        if record.get("state") != "pending":
            if state_name != "admitted" or not any(
                    pr_number_of(item) == int(number) for item in load_queue(queue_path)):
                return record
        record.update({"state": state_name, "reason_code": reason_code,
                       "reason": str(reason), "updated_at": now_utc()})
        record.update(fields)
        state["updated_at"] = now_utc()
        _write_merge_intake_unlocked(queue_path, state)
        return dict(record)


def _pending_intake_records(queue_path):
    with queue_lock(queue_path):
        state = load_merge_intake(queue_path)
    return [dict(row) for row in state["records"].values()
            if row.get("state") == "pending"]


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


# ---------------------------------------------------------------- claim gate
def _unique_item_index(items, pr_number):
    matches = [i for i, item in enumerate(items)
               if pr_number_of(item) == int(pr_number)]
    if len(matches) != 1:
        raise QueueStateError("queue must contain exactly one item for PR #%s" % pr_number)
    return matches[0]


def _canonical_queue_identity(item):
    import application_identity as identity
    try:
        pr = identity.pr_number(item)
    except (TypeError, ValueError) as exc:
        raise QueueStateError("queue item has contradictory PR aliases") from exc
    number = pr_number_of(item)
    if not number or (pr and pr != number):
        raise QueueStateError("queue item has contradictory PR aliases")
    if item.get("pr"):
        match = re.search(r"/pull/([1-9]\d*)(?:[/?#]|$)", str(item.get("pr")))
        if not match or int(match.group(1)) != number:
            raise QueueStateError("queue PR URL does not match its PR number")
    for alias in ("pr_url", "pull_request_url"):
        value = item.get(alias)
        if value:
            match = re.search(r"/pull/([1-9]\d*)(?:[/?#]|$)", str(value))
            if not match or int(match.group(1)) != number:
                raise QueueStateError("queue PR URL aliases disagree")
    link = identity.normalize_posting_url(item.get("link"))
    if not link:
        raise QueueStateError("queue item has no canonical posting link")
    for alias in ("posting_url", "posting_link"):
        value = item.get(alias)
        if value and identity.normalize_posting_url(value) != link:
            raise QueueStateError("queue item has conflicting posting URL aliases")
    app_id = identity.application_id(item)
    if app_id != "link:" + link:
        raise QueueStateError("queue item has an ambiguous canonical identity")
    stored_id = str(item.get("application_id") or "").strip()
    if stored_id and stored_id != app_id:
        raise QueueStateError("queue application_id conflicts with its posting link")
    return app_id


def _read_flat_entries(path, label, required=False):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        entries = parse_queue(text, status_values=None)
        explicit_empty = any(line.strip() == "[]" for line in text.splitlines())
        if required and not entries and not explicit_empty:
            raise QueueStateError("%s has no explicit empty-list marker or entries" % label)
        return entries
    except FileNotFoundError:
        if required:
            raise QueueStateError("%s is unavailable" % label)
        return []
    except (OSError, UnicodeError, QueueStateError) as exc:
        raise QueueStateError("cannot safely read %s" % label) from exc


def _optional_file_hash(path, label, required=False):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        if required:
            raise QueueStateError("%s is unavailable" % label)
        return ""
    except OSError as exc:
        raise QueueStateError("cannot safely read %s" % label) from exc


def _read_gate_entries(path, required=False):
    """Read the small gates.yaml shape, including inline company alias lists."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        if required:
            raise QueueStateError("owner application caps are unavailable")
        return []
    except (OSError, UnicodeError) as exc:
        raise QueueStateError("cannot safely read owner application caps") from exc
    explicit_empty = any(line.strip() == "[]" for line in text.splitlines())
    entries, current = [], None
    in_reason = False
    lines = text.splitlines()
    for line_no, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "[]":
            if entries or current is not None or any(
                    line.strip() and not line.lstrip().startswith("#")
                    for line in lines[line_no:]):
                raise QueueStateError("malformed owner cap file at line %d" % line_no)
            return []
        if raw.startswith("    ") and in_reason:
            continue
        in_reason = False
        if stripped.startswith("- "):
            current = {}
            entries.append(current)
            value_line = stripped[2:].strip()
        elif current is not None and raw.startswith("  ") and not raw.startswith("   "):
            value_line = stripped
        else:
            raise QueueStateError("malformed owner cap file at line %d" % line_no)
        if ":" not in value_line:
            raise QueueStateError("malformed owner cap field at line %d" % line_no)
        key, _, value = value_line.partition(":")
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) or key in current:
            raise QueueStateError("malformed owner cap field at line %d" % line_no)
        if key == "company_aliases":
            if not (value.startswith("[") and value.endswith("]")):
                raise QueueStateError("owner cap aliases must be an inline list")
            content = value[1:-1].strip()
            current[key] = [part.strip().strip("\"'") for part in content.split(",")
                            if part.strip()]
        elif value in (">-", ">", "|", "|-") and key == "reason":
            current[key] = ""
            in_reason = True
        else:
            current[key] = _scalar(value)
    if required and not entries and not explicit_empty:
        raise QueueStateError("owner application caps have no explicit empty-list marker or entries")
    return entries


def _check_owner_controls(posting, skipped, gates):
    from posting_admission import owner_control_exclusion
    reason = owner_control_exclusion(posting, skipped, gates)
    if reason:
        raise QueueStateError(reason)


def _check_claim_identities(item, queue_items, tracker_rows, posting):
    """Apply the same identity gates before and after the network check."""
    import application_identity as identity
    app_id = _canonical_queue_identity(item)
    if sum(1 for row in queue_items
           if identity.normalize_posting_url(row.get("link")) == app_id[5:]) != 1:
        raise QueueStateError("queue posting link has a duplicate or conflicting alias")
    target_index = queue_items.index(item)
    effective = dict(item)
    for key in ("company", "role"):
        if not norm_text(effective.get(key)):
            effective[key] = posting.get(key, "")
    effective_queue = list(queue_items)
    effective_queue[target_index] = effective
    resolution = identity.resolve_applications(tracker_rows, effective_queue)
    target_queue_ref = "queue:%d" % (len(tracker_rows) + target_index + 1)
    if any(target_queue_ref in conflict.get("row_refs", [])
           for conflict in resolution.get("conflicts", [])):
        raise QueueStateError("application identity conflicts with another queue or tracker row")
    for tracker in tracker_rows:
        try:
            tracker_pr = identity.pr_number(tracker)
        except (TypeError, ValueError) as exc:
            raise QueueStateError("tracker contains contradictory PR aliases") from exc
        tracker_link = identity.normalize_posting_url(tracker.get("link"))
        same_id = tracker_link == app_id[5:] if tracker_link else False
        same_pr = tracker_pr and tracker_pr == pr_number_of(item)
        if same_pr and tracker_link and tracker_link != app_id[5:]:
            raise QueueStateError("current tracker PR alias points at a different posting")
        same_name = (norm_text(tracker.get("company")).casefold(),
                     norm_text(tracker.get("role")).casefold()) == (
                         norm_text(posting.get("company")).casefold(),
                         norm_text(posting.get("role")).casefold())
        if (same_id or same_pr or same_name) and not same_pr:
            raise QueueStateError("current tracker contains a conflicting application identity")
        if (same_id or same_pr) and canonical_submitted(tracker.get("status")):
            raise QueueStateError("application is already recorded as submitted")


def _read_claim_inputs(args, item, queue_items):
    import application_identity as identity
    import eligibility
    import tailor
    from posting_guard import guard_text

    app_id = _canonical_queue_identity(item)

    postings_path = os.path.join(args.repo, "data", "postings.json")
    try:
        with open(postings_path, "rb") as fh:
            postings_bytes = fh.read()
        postings = json.loads(postings_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise QueueStateError("current postings are unavailable or malformed") from exc
    if not isinstance(postings, list) or any(not isinstance(row, dict) for row in postings):
        raise QueueStateError("current postings must be a list of objects")
    matches = [row for row in postings
               if identity.normalize_posting_url(row.get("link")) == app_id[5:]]
    if len(matches) != 1:
        raise QueueStateError("posting link does not identify exactly one current posting")
    posting = dict(matches[0])
    clean_meta, meta_verdict, meta_findings = tailor.guard_posting_metadata(posting)
    if (meta_verdict == "hostile" or any(f.get("kind") == "injection"
                                           for f in meta_findings)):
        raise QueueStateError("current posting metadata requires review")
    if not norm_text(clean_meta.get("company")) or not norm_text(clean_meta.get("role")):
        raise QueueStateError("current posting lacks verified company or role metadata")
    posting["company"] = clean_meta["company"]
    posting["role"] = clean_meta["role"]
    posting["title"] = clean_meta.get("title", "")
    for key in ("posting_url", "posting_link"):
        alias = posting.get(key)
        if alias and identity.normalize_posting_url(alias) != app_id[5:]:
            raise QueueStateError("current posting contains a conflicting URL alias")

    profile_path = args.profile or os.path.join(args.repo, "data", "profile.yaml")
    try:
        with open(profile_path, "rb") as fh:
            profile_source = fh.read()
        profile = tailor.parse_yaml_map(profile_source.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise QueueStateError("selected profile is unavailable or malformed") from exc
    if not isinstance(profile, dict) or not profile:
        raise QueueStateError("selected profile is empty")

    tracker_path = os.path.join(args.repo, TRACKER_PATH)
    if not os.path.isfile(tracker_path):
        raise QueueStateError("current application tracker is unavailable")
    tracker_rows = _read_flat_entries(tracker_path, "current application tracker",
                                      required=True)
    _check_claim_identities(item, queue_items, tracker_rows, posting)

    skipped = _read_flat_entries(args.skipped, "owner skip list", required=True)
    gates = _read_gate_entries(args.gates, required=True)
    _check_owner_controls(posting, skipped, gates)
    skip_sha256 = _optional_file_hash(args.skipped, "owner skip list", True)
    gate_sha256 = _optional_file_hash(args.gates, "owner application caps", True)

    fetched = tailor.fetch_jd(posting)
    if not isinstance(fetched, dict) or not isinstance(fetched.get("jd_text"), str) \
            or not fetched["jd_text"].strip():
        raise QueueStateError("a fresh current job description could not be fetched")
    guarded_jd = guard_text(fetched["jd_text"])
    if guarded_jd.get("verdict") == "hostile":
        raise QueueStateError("current job description requires posting-guard review")
    if tailor.is_explicitly_closed("\n".join((posting.get("company", ""),
                                               posting.get("role", ""),
                                               posting.get("title", ""),
                                               guarded_jd["clean_text"]))):
        raise QueueStateError("current posting is explicitly closed")
    decision = eligibility.evaluate(posting, profile, guarded_jd["clean_text"],
                                    profile_source=profile_source)
    if not isinstance(decision, dict) or decision.get("status") != "eligible":
        status = decision.get("status") if isinstance(decision, dict) else "unavailable"
        raise QueueStateError("current eligibility is %s; claim requires eligible" % status)
    return {
        "posting": posting,
        "apply_url": fetched.get("apply_url"),
        "decision": decision,
        "profile_path": os.path.abspath(profile_path),
        "profile_sha256": hashlib.sha256(profile_source).hexdigest(),
        "postings_sha256": hashlib.sha256(postings_bytes).hexdigest(),
        "tracker_rows": tracker_rows,
        "tracker_sha256": _optional_file_hash(tracker_path, "current application tracker", True),
        "skip_sha256": skip_sha256,
        "gate_sha256": gate_sha256,
        "jd_sha256": hashlib.sha256(guarded_jd["clean_text"].encode("utf-8")).hexdigest(),
        "application_id": app_id,
    }


def cmd_claim(args, log):
    try:
        with queue_lock(args.queue):
            initial = load_queue(args.queue)
            idx = _unique_item_index(initial, args.pr)
            item = dict(initial[idx])
            if item.get("status") != "queued":
                raise QueueStateError("only a queued item can be claimed")
            if item.get("attempt_id") or item.get("submit_gate"):
                raise QueueStateError("queued item retains an unresolved attempt marker")
    except QueueStateError as exc:
        log("queue: claim blocked: " + str(exc))
        return 2

    try:
        inputs = _read_claim_inputs(args, item, initial)
    except (OSError, ValueError, RuntimeError) as exc:
        log("queue: claim blocked: " + str(exc))
        return 2

    try:
        with queue_lock(args.queue):
            latest = load_queue(args.queue)
            idx = _unique_item_index(latest, args.pr)
            current = dict(latest[idx])
            if (current.get("status") != "queued" or current.get("attempt_id")
                    or current.get("submit_gate")):
                raise QueueStateError("item changed while eligibility was being checked")
            if _canonical_queue_identity(current) != inputs["application_id"]:
                raise QueueStateError("posting identity changed while eligibility was being checked")
            current_inputs = _read_claim_validation_inputs(args, current, latest,
                                                           inputs["posting"])
            expected_inputs = (inputs["profile_sha256"], inputs["postings_sha256"],
                               inputs["tracker_sha256"], inputs["skip_sha256"],
                               inputs["gate_sha256"])
            if current_inputs != expected_inputs:
                raise QueueStateError("profile, tracker, owner controls, or posting store changed during claim checks")
            for key in ("company", "role"):
                old, verified = norm_text(current.get(key)), norm_text(
                    inputs["posting"].get(key))
                if old and old.casefold() != verified.casefold():
                    raise QueueStateError("queue %s conflicts with the uniquely verified posting" % key)
            repaired = {
                key: norm_text(current.get(key)) for key in ("company", "role", "source")
                if norm_text(current.get(key)) !=
                norm_text(inputs["posting"].get(key))
            }
            if repaired:
                try:
                    repair_history = json.loads(current.get("metadata_repair_history") or "[]")
                except ValueError as exc:
                    raise QueueStateError("metadata repair history is malformed") from exc
                if not isinstance(repair_history, list):
                    raise QueueStateError("metadata repair history is malformed")
                repair_history.append({"at": now_utc(), "from": repaired,
                                       "to": {key: inputs["posting"].get(key)
                                              for key in repaired}})
                current["metadata_repair_history"] = json.dumps(
                    repair_history, sort_keys=True, separators=(",", ":"))
            for key in ("company", "role", "source"):
                if inputs["posting"].get(key):
                    current[key] = norm_text(inputs["posting"].get(key))
            if inputs.get("apply_url") and not current.get("apply_url"):
                current["apply_url"] = norm_text(inputs["apply_url"])
            attempt_id = uuid.uuid4().hex
            current.update({
                "status": "in_progress",
                "attempt_id": attempt_id,
                "attempt_state": "authorized",
                "submit_gate": "durable",
                "claimed_at": now_utc(),
                "claim_application_id": inputs["application_id"],
                "claim_profile_path": inputs["profile_path"],
                "claim_profile_sha256": inputs["profile_sha256"],
                "claim_jd_sha256": inputs["jd_sha256"],
                "eligibility_evidence": json.dumps({
                    "status": inputs["decision"].get("status"),
                    "reason_codes": inputs["decision"].get("reason_codes", []),
                    "provenance": inputs["decision"].get("provenance", {}),
                    "evaluated_at": now_utc(),
                }, sort_keys=True, separators=(",", ":")),
            })
            latest[idx] = current
            _write_queue_unlocked(args.queue, latest)
    except (OSError, QueueStateError) as exc:
        log("queue: claim not authorized: " + str(exc))
        return 2
    log("queue: claim persisted for #%s; external submission gate is durable" % args.pr)
    if args.json:
        print(json.dumps(current, indent=2, ensure_ascii=False))
    else:
        print("claimed: %s" % render_item_line(current))
        print("attempt_id: %s" % attempt_id)
    return 0


def _read_claim_validation_inputs(args, item, queue_items, posting):
    """Cheap local recheck while holding the queue lock; no network calls."""
    import application_identity as identity
    profile_path = args.profile or os.path.join(args.repo, "data", "profile.yaml")
    postings_path = os.path.join(args.repo, "data", "postings.json")
    tracker_path = os.path.join(args.repo, TRACKER_PATH)
    try:
        with open(profile_path, "rb") as fh:
            profile_bytes = fh.read()
        with open(postings_path, "rb") as fh:
            postings_bytes = fh.read()
        with open(tracker_path, "rb") as fh:
            tracker_bytes = fh.read()
    except OSError as exc:
        raise QueueStateError("profile, tracker, or posting store became unavailable") from exc
    if _canonical_queue_identity(item) != identity.application_id(item):
        raise QueueStateError("application identity changed")
    tracker_rows = _read_flat_entries(tracker_path, "current application tracker", required=True)
    _check_claim_identities(item, queue_items, tracker_rows, posting)
    skipped = _read_flat_entries(args.skipped, "owner skip list", required=True)
    gates = _read_gate_entries(args.gates, required=True)
    _check_owner_controls(posting, skipped, gates)
    return (hashlib.sha256(profile_bytes).hexdigest(),
            hashlib.sha256(postings_bytes).hexdigest(),
            hashlib.sha256(tracker_bytes).hexdigest(),
            _optional_file_hash(args.skipped, "owner skip list", True),
            _optional_file_hash(args.gates, "owner application caps", True))


# ---------------------------------------------------------------- cli actions
ENQUEUE_COUNT_KEYS = ("processed", "accepted", "added", "rejected", "duplicates",
                      "limited", "observed", "owner_merged", "closed_history",
                      "active_work")


def _new_enqueue_report():
    return {"outcome": "healthy_empty",
            "counts": {key: 0 for key in ENQUEUE_COUNT_KEYS}, "reasons": {}}


def _count_reason(report, code, amount=1):
    reasons = report["reasons"]
    reasons[code] = reasons.get(code, 0) + amount


def _intake_skip_code(reason):
    if reason == "not an app/* PR":
        return "not_application_pr"
    if reason.startswith("not a tailoring PR"):
        return "not_tailoring_pr"
    if reason == "already queued":
        return "already_queued"
    if reason == "already submitted in tracker":
        return "already_submitted"
    if reason == "already active for this application":
        return "already_active"
    if reason == "limit reached":
        return "limit_reached"
    return "unknown"


def _emit_intake_report(report, log, allow_seam=True, emit_stdout=True):
    if allow_seam:
        try:
            import intake_reporting
        except ImportError:
            intake_reporting = None
        if intake_reporting is not None:
            try:
                intake_reporting.record_run("enqueue", report)
                return True
            except Exception as exc:  # noqa: BLE001 - reporting must not block intake
                log("queue: intake reporting failed: " + str(exc))
    if emit_stdout:
        print(json.dumps({"intake_run": report}, ensure_ascii=False))
    return False


def _read_intake_admission_inputs(repo, tailor):
    postings_path = os.path.join(repo, "data", "postings.json")
    try:
        with open(postings_path, "rb") as fh:
            postings_bytes = fh.read()
        postings = json.loads(postings_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise QueueStateError("current postings are unavailable or malformed") from exc
    if not isinstance(postings, list) or any(not isinstance(row, dict) for row in postings):
        raise QueueStateError("current postings must be a list of objects")
    skipped = _read_flat_entries(tailor.SKIPPED_FILE, "owner skip list", required=True)
    gates = _read_gate_entries(tailor.GATE_FILE, required=True)
    return postings, skipped, gates


def _match_intake_posting(link, postings):
    import application_identity as identity
    canonical = identity.normalize_posting_url(link)
    if not canonical:
        return None, "queue candidate has no verified posting link"
    matches = [row for row in postings
               if identity.normalize_posting_url(row.get("link")) == canonical]
    if len(matches) != 1:
        return None, ("posting link does not identify exactly one current posting")
    return dict(matches[0]), ""


def _intake_admission_decision(args, item):
    """Check fresh posting age, current owner controls and explicit closure."""
    import application_identity as identity
    import tailor
    from posting_admission import admission_exclusion

    try:
        postings, skipped, gates = _read_intake_admission_inputs(args.repo, tailor)
    except (OSError, UnicodeError, QueueStateError, ValueError) as exc:
        return "pending", "source_unavailable", str(exc), False, None
    posting, reason = _match_intake_posting(item.get("link"), postings)
    if posting is None:
        return "excluded", "identity_review", reason, False, None
    try:
        reason = admission_exclusion(posting, skipped, gates)
    except (TypeError, ValueError) as exc:
        return "pending", "source_unavailable", str(exc), False, posting
    if reason:
        return "excluded", "unknown", reason, False, posting

    try:
        fetched = tailor.fetch_jd(posting)
    except Exception as exc:  # noqa: BLE001 - posting fetch is retryable
        return ("pending", "source_unavailable",
                "current job description fetch failed: " + str(exc), False, posting)
    jd_text = fetched.get("jd_text") if isinstance(fetched, dict) else None
    if not isinstance(jd_text, str) or not jd_text.strip():
        return ("pending", "source_unavailable",
                "current job description is unavailable", False, posting)
    closed = tailor.is_explicitly_closed("\n".join((
        str(posting.get("company") or ""), str(posting.get("role") or ""),
        str(posting.get("title") or ""), jd_text)))

    try:
        latest_postings, latest_skipped, latest_gates = _read_intake_admission_inputs(
            args.repo, tailor)
    except (OSError, UnicodeError, QueueStateError, ValueError) as exc:
        return "pending", "source_unavailable", str(exc), closed, posting
    latest, reason = _match_intake_posting(item.get("link"), latest_postings)
    if latest is None:
        return "excluded", "identity_review", reason, closed, posting
    try:
        reason = admission_exclusion(latest, latest_skipped, latest_gates,
                                     explicitly_closed=closed)
    except (TypeError, ValueError) as exc:
        return "pending", "source_unavailable", str(exc), closed, latest
    if reason:
        return "excluded", "unknown", reason, closed, latest
    if latest != posting:
        return ("pending", "source_unavailable",
                "posting changed during admission check", closed, posting)
    app_identity = identity.application_id(item)
    if app_identity != "link:" + identity.normalize_posting_url(item.get("link")):
        return ("excluded", "identity_review",
                "queue candidate has ambiguous posting identity", closed, latest)
    return ("admitted", "unknown",
            "current posting passed age, owner-control and closure checks", closed, latest)


def _final_intake_admission_check(args, item, expected_posting, closed):
    """Re-read local age and owner controls while holding the insertion lock."""
    import tailor
    from posting_admission import admission_exclusion

    try:
        postings, skipped, gates = _read_intake_admission_inputs(args.repo, tailor)
    except (OSError, UnicodeError, QueueStateError, ValueError) as exc:
        return "pending", "source_unavailable", str(exc)
    posting, reason = _match_intake_posting(item.get("link"), postings)
    if posting is None:
        return "excluded", "identity_review", reason
    try:
        reason = admission_exclusion(posting, skipped, gates,
                                     explicitly_closed=closed)
    except (TypeError, ValueError) as exc:
        return "pending", "source_unavailable", str(exc)
    if reason:
        return "excluded", "unknown", reason
    if posting != expected_posting:
        return "pending", "source_unavailable", "posting changed before queue insertion"
    return "admitted", "unknown", "current posting passed final admission check"


def _read_fixture_inputs(args, log):
    if not args.prs or not args.tracker:
        log("queue: --dry-run requires offline --prs and --tracker fixtures")
        return None, None
    try:
        with open(args.prs, encoding="utf-8") as fh:
            prs = json.load(fh)
        with open(args.tracker, encoding="utf-8") as fh:
            tracker_text = fh.read()
    except (OSError, ValueError) as exc:
        log(f"queue: cannot read dry-run fixtures: {exc}")
        return None, None
    if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
        log("queue: --prs must contain a JSON list of PR objects")
        return None, None
    try:
        parse_queue(tracker_text, status_values=None)
    except QueueStateError as exc:
        log("queue: fixture tracker is malformed: " + str(exc))
        return None, None
    return prs, tracker_text


def _print_enqueue_result(args, added, skipped, report, include_summary=False):
    if args.json:
        print(json.dumps({"added": added, "skipped": [
            {"pr_number": n, "reason": r} for n, r in skipped],
            "summary": report}, indent=2, ensure_ascii=False))
        return
    for it in added:
        print(f"queued: {render_item_line(it)}")
    if include_summary:
        print(json.dumps({"intake_run": report}, ensure_ascii=False))


def _process_pending_intake(args, log, report):
    import tailor

    added_rows, skipped_rows = [], []
    failures = False
    tracker_loaded = False
    tracker_text = None
    records = _pending_intake_records(args.queue)
    records.sort(key=lambda row: (str(row.get("pr", {}).get("mergedAt") or ""),
                                  int(row.get("pr_number") or 0)))
    for record in records:
        number = int(record["pr_number"])
        counts = report["counts"]
        counts["processed"] += 1

        try:
            with queue_lock(args.queue):
                current = load_queue(args.queue)
            if any(pr_number_of(item) == number for item in current):
                _set_intake_outcome(args.queue, number, "admitted", "already_queued",
                                    "queue insertion already exists")
                counts["duplicates"] += 1
                _count_reason(report, "already_queued")
                continue
        except (QueueStateError, OSError) as exc:
            _set_intake_outcome(args.queue, number, "pending", "queue_unavailable",
                                "queue could not be read: " + str(exc))
            _count_reason(report, "queue_unavailable")
            failures = True
            continue

        pr = dict(record.get("pr") or {})
        merge_commit = pr.get("mergeCommit")
        merge_sha = (merge_commit.get("oid") if isinstance(merge_commit, dict) else "")
        if (not norm_ts(pr.get("mergedAt")) or not pr.get("headRefName")
                or not pr.get("headRefOid") or not merge_sha or not pr.get("url")):
            _set_intake_outcome(args.queue, number, "pending", "metadata_unavailable",
                                "authoritative merge or PR identity metadata is incomplete")
            _count_reason(report, "metadata_unavailable")
            failures = True
            continue
        if not is_app_branch(pr.get("headRefName")):
            _set_intake_outcome(args.queue, number, "excluded", "not_application_pr",
                                "merged PR is outside the app/ branch")
            counts["rejected"] += 1
            _count_reason(report, "not_application_pr")
            continue

        files = record.get("files")
        if files is None:
            files = gh_pr_files(number, log)
            if files is None:
                _set_intake_outcome(args.queue, number, "pending", "metadata_unavailable",
                                    "changed-file metadata is unavailable; retry required")
                _count_reason(report, "metadata_unavailable")
                failures = True
                continue
            _set_intake_outcome(args.queue, number, "pending", "unknown",
                                "changed-file metadata saved for replay", files=files,
                                files_observed_at=now_utc())
        if (not isinstance(files, list) or any(not isinstance(file, dict)
                                               or not isinstance(file.get("path"), str)
                                               for file in files)):
            _set_intake_outcome(args.queue, number, "pending", "metadata_unavailable",
                                "changed-file metadata is malformed; retry required")
            _count_reason(report, "metadata_unavailable")
            failures = True
            continue
        pr["files"] = files
        if not variant_from_files(files, pr.get("headRefName")):
            _set_intake_outcome(args.queue, number, "excluded", "not_tailoring_pr",
                                "verified changed-file list contains no tailoring resume")
            counts["rejected"] += 1
            _count_reason(report, "not_tailoring_pr")
            continue

        if args.limit and counts["added"] >= args.limit:
            _set_intake_outcome(args.queue, number, "pending", "limit_reached",
                                "run limit reached; pending for the next intake run")
            counts["limited"] += 1
            _count_reason(report, "limit_reached")
            continue

        if not tracker_loaded:
            tracker_text = read_main_tracker(args.repo, log)
            tracker_loaded = True
            if tracker_text is not None:
                try:
                    parse_queue(tracker_text, status_values=None)
                except QueueStateError:
                    tracker_text = None
        if tracker_text is None:
            _set_intake_outcome(args.queue, number, "pending", "tracker_unavailable",
                                "current application tracker is unavailable or malformed")
            _count_reason(report, "tracker_unavailable")
            failures = True
            continue

        pr["body"] = pr.get("posting_url") or ""
        pr["_tracker"] = tracker_text
        try:
            with queue_lock(args.queue):
                current = load_queue(args.queue)
            preview_items, preview_added, preview_skipped = enqueue(
                current, [pr], args.include_submitted, limit=0)
        except (QueueStateError, OSError, ValueError) as exc:
            _set_intake_outcome(args.queue, number, "pending", "queue_unavailable",
                                "queue candidate check failed: " + str(exc))
            _count_reason(report, "queue_unavailable")
            failures = True
            continue
        if preview_skipped:
            _number, skip_reason = preview_skipped[0]
            skip_code = _intake_skip_code(skip_reason)
            outcome = "admitted" if skip_code == "already_queued" else "excluded"
            _set_intake_outcome(args.queue, number, outcome, skip_code, skip_reason)
            skipped_rows.append((number, skip_reason))
            _count_reason(report, skip_code)
            if outcome == "excluded":
                counts["rejected"] += 1
            if skip_code in {"already_queued", "already_submitted", "already_active"}:
                counts["duplicates"] += 1
            continue
        if len(preview_added) != 1:
            _set_intake_outcome(args.queue, number, "pending", "unknown",
                                "candidate could not be prepared for admission")
            _count_reason(report, "unknown")
            failures = True
            continue
        candidate = preview_added[0]
        if not norm_text(candidate.get("link")):
            _set_intake_outcome(args.queue, number, "excluded", "identity_review",
                                "tailoring merge has no verified posting identity")
            counts["rejected"] += 1
            _count_reason(report, "identity_review")
            continue

        decision, code, reason, closed, expected_posting = _intake_admission_decision(
            args, candidate)
        if decision != "admitted":
            _set_intake_outcome(args.queue, number, decision, code, reason)
            _count_reason(report, code)
            if decision == "excluded":
                counts["rejected"] += 1
            else:
                failures = True
            continue

        try:
            final_admission = None
            with queue_lock(args.queue):
                latest = load_queue(args.queue)
                merged_items, added, final_skips = enqueue(
                    latest, [pr], args.include_submitted, limit=1)
                if added:
                    final_admission = _final_intake_admission_check(
                        args, candidate, expected_posting, closed)
                    if final_admission[0] == "admitted":
                        _write_queue_unlocked(args.queue, merged_items)
                    else:
                        added = []
                        merged_items = latest
            if final_admission and final_admission[0] != "admitted":
                final_state, final_code, final_reason = final_admission
                _set_intake_outcome(args.queue, number, final_state,
                                    final_code, final_reason)
                _count_reason(report, final_code)
                if final_state == "excluded":
                    counts["rejected"] += 1
                else:
                    failures = True
                continue
            if added:
                import application_identity as identity
                merged_identity = dict(record.get("identity") or {})
                merged_identity["application_id"] = identity.application_id(added[0])
                _set_intake_outcome(args.queue, number, "admitted", "unknown",
                                    "owner merge accepted into the application queue",
                                    identity=merged_identity)
                added_rows.extend(added)
                counts["accepted"] += 1
                counts["added"] += 1
            elif final_skips:
                _number, skip_reason = final_skips[0]
                skip_code = _intake_skip_code(skip_reason)
                outcome = "admitted" if skip_code == "already_queued" else "excluded"
                _set_intake_outcome(args.queue, number, outcome, skip_code, skip_reason)
                skipped_rows.append((number, skip_reason))
                _count_reason(report, skip_code)
                if outcome == "excluded":
                    counts["rejected"] += 1
                if skip_code in {"already_queued", "already_submitted", "already_active"}:
                    counts["duplicates"] += 1
        except (QueueStateError, OSError, ValueError) as exc:
            _set_intake_outcome(args.queue, number, "pending", "queue_unavailable",
                                "queue insertion could not be confirmed: " + str(exc))
            _count_reason(report, "queue_unavailable")
            failures = True

    return added_rows, skipped_rows, failures


def cmd_enqueue(args, log):
    report = _new_enqueue_report()
    if (args.prs or args.tracker) and not args.dry_run:
        log("queue: offline inputs require --dry-run; only a GitHub-observed owner merge authorizes enqueue")
        return 2
    if args.dry_run and (not args.prs or not args.tracker):
        _emit_intake_report(report, log, allow_seam=False)
        log("queue: --dry-run requires offline --prs and --tracker fixtures")
        return 2
    try:
        if args.dry_run:
            initial_items = load_queue(args.queue)
        else:
            with queue_lock(args.queue):
                initial_items = load_queue(args.queue)
    except QueueStateError as exc:
        report["outcome"] = "failed"
        _emit_intake_report(report, log, allow_seam=not args.dry_run)
        log("queue: enqueue blocked: " + str(exc))
        return 1

    if args.dry_run:
        prs, tracker_text = _read_fixture_inputs(args, log)
        if prs is None:
            report["outcome"] = "failed"
            _emit_intake_report(report, log, allow_seam=False)
            return 1
        prs_with_tracker, unavailable = [], []
        for pr in prs:
            if pr.get("files") is None and is_app_branch(pr.get("headRefName")):
                unavailable.append((int(pr.get("number") or 0),
                                    "changed-file metadata unavailable in offline fixture"))
                report["counts"]["processed"] += 1
                _count_reason(report, "metadata_unavailable")
                continue
            prs_with_tracker.append(dict(pr, _tracker=tracker_text))
        merged_items, added, skipped = enqueue(initial_items, prs_with_tracker,
                                                args.include_submitted, args.limit)
        report["counts"]["added"] = len(added)
        report["counts"]["accepted"] = len(added)
        report["counts"]["rejected"] = len(skipped)
        report["counts"]["observed"] = len(prs)
        report["counts"]["owner_merged"] = len(prs)
        report["counts"]["active_work"] = sum(
            1 for row in initial_items if row.get("status") in APPLICATION_GUARD_STATUSES)
        for number, reason in skipped:
            _count_reason(report, _intake_skip_code(reason))
        for _number, _reason in unavailable:
            pass
        report["counts"]["limited"] = report["reasons"].get("limit_reached", 0)
        report["outcome"] = "partial_failure" if unavailable else (
            "healthy" if added else "healthy_empty")
        if args.json:
            skipped = skipped + unavailable
        _emit_intake_report(report, log, allow_seam=False, emit_stdout=False)
        _print_enqueue_result(args, added, skipped, report, include_summary=True)
        return 0 if not unavailable else 1

    try:
        load_merge_intake(args.queue)
    except QueueStateError as exc:
        report["outcome"] = "failed"
        _emit_intake_report(report, log)
        log("queue: merge intake state is unavailable: " + str(exc))
        return 1

    def checkpoint_page(page):
        report["counts"]["observed"] += len(page)
        owner = GH_REPO.split("/", 1)[0].casefold()
        for pr in page:
            merged_by = pr.get("mergedBy")
            login = (merged_by.get("login") if isinstance(merged_by, dict) else "")
            if str(login or "").casefold() != owner:
                continue
            report["counts"]["owner_merged"] += 1
            if not is_app_branch(pr.get("headRefName")):
                _count_reason(report, "not_application_pr")
                report["counts"]["rejected"] += 1
        _record_observed_page(args.queue, page)

    listing_failed = False
    try:
        prs = gh_merged_prs(log, on_page=checkpoint_page)
    except (QueueStateError, OSError, ValueError) as exc:
        log("queue: cannot checkpoint merged PR page: " + str(exc))
        prs = None
    if prs is None:
        listing_failed = True
        _count_reason(report, "metadata_unavailable")
    elif not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
        listing_failed = True
        _count_reason(report, "metadata_unavailable")
        log("queue: merged PR response is malformed")
    else:
        if not prs:
            _count_reason(report, "no_merged_prs")
        elif not report["counts"]["owner_merged"]:
            _count_reason(report, "no_owner_merges")

    try:
        added, skipped, pending_failed = _process_pending_intake(args, log, report)
    except (QueueStateError, OSError, ValueError) as exc:
        log("queue: merge intake processing failed: " + str(exc))
        report["outcome"] = "failed"
        _emit_intake_report(report, log)
        return 1
    report["counts"]["closed_history"] = sum(
        1 for row in load_merge_intake(args.queue)["records"].values()
        if row.get("state") == "excluded")
    try:
        with queue_lock(args.queue):
            current = load_queue(args.queue)
        report["counts"]["active_work"] = sum(
            1 for row in current if row.get("status") in APPLICATION_GUARD_STATUSES)
    except (QueueStateError, OSError):
        _count_reason(report, "queue_unavailable")
        pending_failed = True

    if (report["counts"]["processed"] and not report["counts"]["added"]
            and not report["counts"]["limited"]
            and not pending_failed and not listing_failed):
        _count_reason(report, "no_eligible_prs")
    failures = listing_failed or pending_failed
    report["outcome"] = ("partial_failure" if report["counts"]["added"]
                         or report["counts"]["rejected"] else "total_failure") \
        if failures else ("healthy" if report["counts"]["added"] else "healthy_empty")
    _emit_intake_report(report, log, emit_stdout=not args.json)
    _print_enqueue_result(args, added, skipped, report)
    return 1 if failures else 0


def enqueue(existing, prs_with_trackers, include_submitted=False, limit=0):
    """Pure merge: returns ``(items, added, skipped)``.

    Each PR mapping carries ``files`` (its changed paths) and ``_tracker`` (the
    ``applications.yaml`` text at its merge commit). Idempotent by PR number.
    """
    items = [dict(it) for it in existing]
    seen = {pr_number_of(it) for it in items}
    seen.discard(0)
    active = {active_key(it) for it in items
              if it.get("status") in APPLICATION_GUARD_STATUSES}
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
        if active_key(item) in active:
            skipped.append((number, "already active for this application"))
            continue
        if limit and len(added) >= limit:
            skipped.append((number, "limit reached"))
            continue
        seen.add(number)
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
    """Move stale claims to outcome_unknown; never release them for retry."""
    try:
        with queue_lock(args.queue):
            items = load_queue(args.queue)
            changed = []
            for i, it in enumerate(items):
                if not claim_is_stale(it):
                    continue
                row = dict(it)
                row.update({"status": "outcome_unknown",
                            "attempt_state": "outcome_unknown",
                            "outcome_unknown_at": now_utc(),
                            "next_action": "reconcile"})
                if not row.get("attempt_id"):
                    row["attempt_id"] = "legacy-" + uuid.uuid4().hex
                items[i] = row
                changed.append(row)
            if changed and not args.dry_run:
                _write_queue_unlocked(args.queue, items)
    except (QueueStateError, OSError) as exc:
        log("queue: stale-claim recovery failed closed: " + str(exc))
        return 1
    if not changed:
        log("queue: no stale claims")
        return 0
    for row in changed:
        verb = "would mark unknown" if args.dry_run else "marked unknown"
        log("queue: %s #%s %s; reconcile before retry" %
            (verb, pr_number_of(row), row.get("company", "")))
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
    new = args.status
    if new not in ("submitted", "failed"):
        log("queue: status cannot claim or reopen an item; use claim or reconcile")
        return 2
    if args.dry_run:
        log("queue: attempt outcomes must be durably recorded; --dry-run is not allowed")
        return 2
    if args.force:
        log("queue: --force cannot override attempt, eligibility, or terminal-state gates")
        return 2
    if new == "submitted":
        if not args.date or not args.sent or not args.attempt_id \
                or not args.confirmation_evidence:
            log("queue: submitted requires --attempt-id, --date, --sent, and "
                "--confirmation-evidence")
            return 2
        if not norm_text(args.sent):
            log("queue: --sent must contain the exact submitted summary")
            return 2
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            log("queue: --date must be YYYY-MM-DD")
            return 2
    elif not args.notes:
        log("queue: failed requires --notes")
        return 2
    if new == "submitted" and not _valid_confirmation_evidence(args.confirmation_evidence):
        log("queue: confirmation evidence must describe the observed success")
        return 2
    if new == "submitted":
        try:
            import submission_snapshot
            submission_snapshot.validate_confirmation_evidence(args.confirmation_evidence)
        except (ImportError, ValueError) as exc:
            log("queue: confirmation evidence cannot be safely recorded: " + str(exc))
            return 2

    try:
        with queue_lock(args.queue):
            items = load_queue(args.queue)
            idx = _unique_item_index(items, args.pr)
            it = dict(items[idx])
            cur = it.get("status", "queued")
            if new == "failed":
                if cur != "queued" or it.get("attempt_id"):
                    log("queue: a claimed attempt needs outcome_unknown reconciliation")
                    return 2
                it["status"] = "failed"
                append_note(it, args.notes)
            else:
                if (cur != "in_progress" or it.get("attempt_state") != "authorized"
                        or it.get("submit_gate") != "durable"):
                    log("queue: submitted requires the active durable claim")
                    return 2
                if it.get("attempt_id") != args.attempt_id:
                    log("queue: attempt ID does not match the durable claim")
                    return 2
                if claim_is_stale(it):
                    log("queue: stale claims require reconciliation before confirmation")
                    return 2
                # Confirmation is terminal bookkeeping. Never require a new
                # pre-submit check after the external action has succeeded.
                it["snapshot_verification_at_confirmation"] = _verification_state(it)
                it.update({
                    "status": "submitted",
                    "attempt_state": "submitted",
                    "submitted_at": args.date,
                    "sent": norm_text(args.sent),
                    "confirmation_evidence": norm_text(args.confirmation_evidence),
                    "tracker_sync": "pending",
                    "tracker_sync_error": "",
                    "route": norm_text(args.route),
                    "submitted_attempt_id": args.attempt_id,
                    "snapshot_provenance": "pending",
                    "snapshot_error": "",
                })
                it.pop("next_action", None)
            items[idx] = it
            _write_queue_unlocked(args.queue, items)
    except (QueueStateError, OSError) as exc:
        log("queue: status write failed: " + str(exc))
        return 2

    snapshot_failure = (new == "submitted" and
                        _post_submission_snapshot(args, log))
    if new == "submitted" and not args.no_tracker:
        sync_rc = sync_tracker_attempt(args.queue, args.pr, args.repo, log)
    else:
        sync_rc = 0
    latest = load_queue(args.queue)
    it = latest[_unique_item_index(latest, args.pr)]
    log("queue: #%s -> %s" % (args.pr, new))
    if args.json:
        print(json.dumps(it, indent=2, ensure_ascii=False))
    else:
        print("%s: %s" % (new, render_item_line(it)))
    return 1 if sync_rc or snapshot_failure else 0


def _snapshot_claim(queue_path, pr_number, attempt_id):
    """Read one live claim; callers perform snapshot I/O outside the lock."""
    with queue_lock(queue_path):
        rows = load_queue(queue_path)
        item = dict(rows[_unique_item_index(rows, pr_number)])
        if (item.get("status") != "in_progress"
                or item.get("attempt_state") != "authorized"
                or item.get("submit_gate") != "durable"
                or item.get("attempt_id") != attempt_id):
            raise QueueStateError("snapshot requires the exact active durable attempt")
        if claim_is_stale(item):
            raise QueueStateError("stale claims require reconciliation before snapshot work")
        application_id = _canonical_queue_identity(item)
        if item.get("claim_application_id") != application_id:
            raise QueueStateError("claim application identity no longer matches the queue item")
        return item, application_id


def _snapshot_store(root, repo):
    import submission_snapshot
    return submission_snapshot.ensure_external_storage(
        root, os.path.dirname(HERE), repo)


def _clear_snapshot_verification(queue_path, pr_number, attempt_id,
                                 application_id, snapshot_id):
    """Invalidate any old pre-submit check before reading current materials."""
    with queue_lock(queue_path):
        rows = load_queue(queue_path)
        idx = _unique_item_index(rows, pr_number)
        current = dict(rows[idx])
        if (current.get("status") != "in_progress"
                or current.get("attempt_id") != attempt_id
                or current.get("prepared_snapshot_id") != snapshot_id
                or current.get("prepared_snapshot_application_id") != application_id
                or current.get("prepared_snapshot_attempt_id") != attempt_id
                or _canonical_queue_identity(current) != application_id
                or claim_is_stale(current)):
            raise QueueStateError("attempt or prepared snapshot changed before verification")
        for key in ("snapshot_verified_id", "snapshot_verified_application_id",
                    "snapshot_verified_attempt_id", "snapshot_verified_at"):
            current.pop(key, None)
        current["snapshot_provenance"] = "prepared"
        rows[idx] = current
        _write_queue_unlocked(queue_path, rows)


def cmd_prepare_snapshot(args, log):
    try:
        import submission_snapshot as snapshots
        item, application_id = _snapshot_claim(args.queue, args.pr, args.attempt_id)
        # A failed re-prepare cannot leave an older verification authorizing Submit.
        old_id = item.get("prepared_snapshot_id")
        if old_id:
            _clear_snapshot_verification(args.queue, args.pr, args.attempt_id,
                                         application_id, old_id)
        root = _snapshot_store(SNAPSHOT_ROOT, args.repo)
        prepared = snapshots.prepare_snapshot(
            args.pdf, args.answers, root, application_id, args.attempt_id,
            args.operator_verification, snapshots.source_commits(args.repo, item))
        with queue_lock(args.queue):
            rows = load_queue(args.queue)
            idx = _unique_item_index(rows, args.pr)
            current = dict(rows[idx])
            if (current.get("status") != "in_progress"
                    or current.get("attempt_id") != args.attempt_id
                    or _canonical_queue_identity(current) != application_id
                    or current.get("claim_application_id") != application_id
                    or claim_is_stale(current)):
                raise QueueStateError("attempt changed while the snapshot was being prepared")
            current.update({
                "prepared_snapshot_id": prepared["snapshot_id"],
                "prepared_snapshot_application_id": application_id,
                "prepared_snapshot_attempt_id": args.attempt_id,
                "prepared_snapshot_at": prepared["captured_at"],
                "snapshot_provenance": "prepared",
                "snapshot_error": "",
            })
            for key in ("snapshot_verified_id", "snapshot_verified_application_id",
                        "snapshot_verified_attempt_id", "snapshot_verified_at"):
                current.pop(key, None)
            rows[idx] = current
            _write_queue_unlocked(args.queue, rows)
    except (OSError, QueueStateError, ValueError) as exc:
        log("queue: snapshot preparation failed: " + str(exc))
        return 2
    log("queue: prepared private snapshot %s for #%s" % (prepared["snapshot_id"], args.pr))
    return 0


def cmd_verify_snapshot(args, log):
    try:
        import submission_snapshot as snapshots
        item, application_id = _snapshot_claim(args.queue, args.pr, args.attempt_id)
        snapshot_id = item.get("prepared_snapshot_id")
        if (item.get("prepared_snapshot_application_id") != application_id
                or item.get("prepared_snapshot_attempt_id") != args.attempt_id
                or not snapshot_id):
            raise QueueStateError("no prepared snapshot belongs to this application and attempt")
        _clear_snapshot_verification(args.queue, args.pr, args.attempt_id,
                                     application_id, snapshot_id)
        root = _snapshot_store(SNAPSHOT_ROOT, args.repo)
        snapshots.verify_snapshot(root, snapshot_id, application_id, args.attempt_id,
                                  args.pdf, args.answers)
        with queue_lock(args.queue):
            rows = load_queue(args.queue)
            idx = _unique_item_index(rows, args.pr)
            current = dict(rows[idx])
            if (current.get("status") != "in_progress"
                    or current.get("attempt_id") != args.attempt_id
                    or current.get("prepared_snapshot_id") != snapshot_id
                    or current.get("prepared_snapshot_application_id") != application_id
                    or current.get("prepared_snapshot_attempt_id") != args.attempt_id
                    or _canonical_queue_identity(current) != application_id
                    or claim_is_stale(current)):
                raise QueueStateError("attempt or prepared snapshot changed during verification")
            current.update({
                "snapshot_verified_id": snapshot_id,
                "snapshot_verified_application_id": application_id,
                "snapshot_verified_attempt_id": args.attempt_id,
                "snapshot_verified_at": now_utc(),
                "snapshot_provenance": "verified",
                "snapshot_error": "",
            })
            rows[idx] = current
            _write_queue_unlocked(args.queue, rows)
    except (OSError, QueueStateError, ValueError) as exc:
        log("queue: snapshot verification failed: " + str(exc))
        return 2
    log("queue: verified retained and selected materials for snapshot %s" % snapshot_id)
    return 0


def _verification_state(item):
    """Describe the recorded check without inventing a post-submit check."""
    if (not item.get("prepared_snapshot_id")
            or item.get("snapshot_verified_application_id") != _canonical_queue_identity(item)
            or item.get("snapshot_verified_attempt_id") != item.get("attempt_id")
            or item.get("snapshot_verified_id") != item.get("prepared_snapshot_id")
            or not norm_ts(item.get("snapshot_verified_at"))):
        return "missing_or_mismatched"
    return "fresh" if snapshot_verification_is_fresh(item) else "expired"


def _post_submission_snapshot(args, log):
    """Shared post-confirmation repair; the durable outcome never reopens."""
    try:
        _finalize_snapshot_attempt(args.queue, args.pr, args.attempt_id, args.repo)
        return False
    except (OSError, QueueStateError, ValueError):
        _record_snapshot_failure(args.queue, args.pr, args.attempt_id)
        log("queue: submission is recorded; snapshot provenance needs attention. "
            "Inspect snapshot_error; repair retained evidence with queue.py "
            "finalize-snapshot --pr %s --attempt-id %s. Never resubmit."
            % (args.pr, args.attempt_id))
        return True


def _finalize_snapshot_attempt(queue_path, pr_number, attempt_id, private_repo=None):
    """Finalize only an already submitted row; never creates a submission."""
    import submission_snapshot as snapshots
    with queue_lock(queue_path):
        rows = load_queue(queue_path)
        idx = _unique_item_index(rows, pr_number)
        item = dict(rows[idx])
        if (item.get("status") != "submitted"
                or item.get("attempt_id") != attempt_id
                or item.get("submitted_attempt_id") != attempt_id):
            raise QueueStateError("snapshot finalization requires the matching submitted attempt")
        application_id = _canonical_queue_identity(item)
        snapshot_id = item.get("prepared_snapshot_id")
        if (not snapshot_id or item.get("prepared_snapshot_attempt_id") != attempt_id
                or item.get("prepared_snapshot_application_id") != application_id):
            raise QueueStateError("no prepared snapshot exists for this submitted attempt")
        submitted_at = item.get("submitted_at")
        evidence = item.get("confirmation_evidence")
    root = _snapshot_store(SNAPSHOT_ROOT, private_repo)
    finalized_id = snapshots.finalize_snapshot(root, snapshot_id, application_id,
                                                attempt_id, submitted_at, evidence)
    with queue_lock(queue_path):
        rows = load_queue(queue_path)
        idx = _unique_item_index(rows, pr_number)
        current = dict(rows[idx])
        if (current.get("status") != "submitted"
                or current.get("attempt_id") != attempt_id
                or current.get("submitted_attempt_id") != attempt_id
                or current.get("prepared_snapshot_id") != snapshot_id
                or _canonical_queue_identity(current) != application_id):
            raise QueueStateError("submitted attempt changed during snapshot finalization")
        verified = _verification_state(current) != "missing_or_mismatched"
        current.update({"snapshot_id": finalized_id,
                        "snapshot_provenance": "complete" if verified else "incomplete",
                        "snapshot_finalized_at": now_utc(),
                        "snapshot_error": "" if verified else (
                            "Pre-submit verification was not recorded for these materials; "
                            "retained bytes are finalized, but historical verification "
                            "cannot be recreated. Never resubmit.")})
        rows[idx] = current
        _write_queue_unlocked(queue_path, rows)
    if not verified:
        raise QueueStateError("pre-submit verification is missing")


def _record_snapshot_failure(queue_path, pr_number, attempt_id):
    """Keep a terminal submission and prepared reference after a storage fault."""
    try:
        with queue_lock(queue_path):
            rows = load_queue(queue_path)
            idx = _unique_item_index(rows, pr_number)
            current = dict(rows[idx])
            if (current.get("status") != "submitted"
                    or current.get("attempt_id") != attempt_id
                    or current.get("submitted_attempt_id") != attempt_id):
                return
            if not current.get("prepared_snapshot_id"):
                current["snapshot_provenance"] = "unknown"
                current["snapshot_error"] = (
                    "No retained snapshot exists for this submitted attempt; "
                    "historical materials cannot be recreated. Never resubmit.")
            elif (current.get("snapshot_id")
                  and _verification_state(current) == "missing_or_mismatched"):
                current["snapshot_provenance"] = "incomplete"
                current["snapshot_error"] = (
                    "Pre-submit verification is missing; historical verification "
                    "cannot be recreated. Never resubmit.")
            else:
                current["snapshot_provenance"] = "incomplete"
                current["snapshot_error"] = (
                    "Private snapshot finalization failed; retain this submitted attempt, "
                    "restore snapshot storage, then run queue.py finalize-snapshot "
                    "--pr %s --attempt-id %s." % (pr_number, attempt_id))
            rows[idx] = current
            _write_queue_unlocked(queue_path, rows)
    except (OSError, QueueStateError):
        # The submitted write already succeeded. A later repair pass can inspect it.
        return


def cmd_finalize_snapshot(args, log):
    try:
        _finalize_snapshot_attempt(args.queue, args.pr, args.attempt_id, args.repo)
    except (OSError, QueueStateError, ValueError) as exc:
        _record_snapshot_failure(args.queue, args.pr, args.attempt_id)
        log("queue: submitted attempt remains terminal; snapshot repair failed: " + str(exc))
        return 1
    log("queue: finalized snapshot provenance for submitted attempt #%s" % args.pr)
    return 0


def _concrete_reconciliation_evidence(args, expected):
    source = norm_text(args.evidence_source)
    observation = norm_text(args.evidence_observation)
    if len(source) < 8 or len(observation) < 20:
        raise QueueStateError("reconciliation needs a specific evidence source and observation")
    import submission_snapshot
    # Apply the same credential filter to every free-form evidence field.
    submission_snapshot.validate_confirmation_evidence(source + ". " + observation)
    if source.casefold() in {"review", "manual review", "notes", "operator", "user"}:
        raise QueueStateError("generic review notes are not reconciliation evidence")
    if not re.search(r"\b(portal|history|email|confirmation|receipt|ATS|candidate|employer)\b|https?://",
                     source, re.I):
        raise QueueStateError("evidence source must name a portal, history, message, or record")
    if expected == "not_submitted":
        if not re.search(r"\b(no application|no submission|not submitted|no record|"
                         r"draft only|still in draft|submission absent)\b",
                         observation, re.I):
            raise QueueStateError("retry evidence must state a concrete non-submission finding")
    elif not re.search(r"\b(submitted|submission received|application received|"
                       r"confirmation number)\b", observation, re.I):
        raise QueueStateError("confirmed evidence must describe an observed submission")
    return source, observation


def _valid_confirmation_evidence(value):
    text = norm_text(value)
    return (len(text) >= 12 and bool(re.search(
        r"\b(submitted|submission received|application received|confirmation|"
        r"receipt|successfully applied|thank you for applying)\b", text, re.I)))


def _append_attempt_history(item, evidence):
    try:
        history = json.loads(item.get("attempt_history") or "[]")
    except ValueError as exc:
        raise QueueStateError("attempt history is malformed") from exc
    if not isinstance(history, list):
        raise QueueStateError("attempt history is malformed")
    history.append({
        "attempt_id": item.get("attempt_id", ""),
        "claimed_at": item.get("claimed_at", ""),
        "resolved_at": now_utc(),
        "reconciliation": evidence,
    })
    item["attempt_history"] = json.dumps(history, sort_keys=True,
                                         separators=(",", ":"))


def cmd_reconcile(args, log):
    try:
        evidence_source, evidence_observation = _concrete_reconciliation_evidence(
            args, "not_submitted" if args.result == "retry" else "submitted")
        if args.result == "submitted":
            if not args.date or not args.sent or not args.confirmation_evidence:
                raise QueueStateError("submitted reconciliation requires date, sent, and confirmation evidence")
            datetime.strptime(args.date, "%Y-%m-%d")
            if not _valid_confirmation_evidence(args.confirmation_evidence):
                raise QueueStateError("confirmation evidence must describe the observed success")
            import submission_snapshot
            submission_snapshot.validate_confirmation_evidence(args.confirmation_evidence)
        with queue_lock(args.queue):
            items = load_queue(args.queue)
            idx = _unique_item_index(items, args.pr)
            item = dict(items[idx])
            if item.get("status") != "outcome_unknown":
                raise QueueStateError("reconcile applies only to outcome_unknown attempts")
            if not item.get("attempt_id") or item.get("attempt_id") != args.attempt_id:
                raise QueueStateError("attempt ID does not match the unresolved attempt")
            evidence = {
                "kind": "not_submitted" if args.result == "retry" else "submission_confirmed",
                "source": evidence_source,
                "observation": evidence_observation,
                "checked_at": now_utc(),
            }
            item["reconciliation_evidence"] = json.dumps(
                evidence, sort_keys=True, separators=(",", ":"))
            if args.result == "retry":
                _append_attempt_history(item, evidence)
                for key in ("attempt_id", "attempt_state", "submit_gate", "claimed_at",
                            "outcome_unknown_at", "next_action", "claim_application_id",
                            "claim_profile_path", "claim_profile_sha256", "claim_jd_sha256",
                            "eligibility_evidence"):
                    item.pop(key, None)
                item["status"] = "queued"
            else:
                item["snapshot_verification_at_confirmation"] = _verification_state(item)
                item.update({
                    "status": "submitted",
                    "attempt_state": "submitted",
                    "submitted_at": args.date,
                    "sent": norm_text(args.sent),
                    "confirmation_evidence": norm_text(args.confirmation_evidence),
                    "submitted_attempt_id": args.attempt_id,
                    "tracker_sync": "pending",
                    "tracker_sync_error": "",
                    "route": norm_text(args.route),
                    "snapshot_provenance": "pending",
                    "snapshot_error": "",
                })
                item.pop("next_action", None)
            items[idx] = item
            _write_queue_unlocked(args.queue, items)
    except (OSError, QueueStateError, ValueError) as exc:
        log("queue: reconciliation blocked: " + str(exc))
        return 2
    if args.result != "submitted":
        return 0
    snapshot_failure = _post_submission_snapshot(args, log)
    sync_rc = (0 if args.no_tracker else
               sync_tracker_attempt(args.queue, args.pr, args.repo, log))
    return 1 if snapshot_failure or sync_rc else 0


def sync_tracker_attempt(queue_path, pr_number, repo, log, dry_run=False):
    """Write only tracker bookkeeping, then merge the result for this attempt."""
    sync_id = uuid.uuid4().hex
    try:
        with queue_lock(queue_path):
            items = load_queue(queue_path)
            idx = _unique_item_index(items, pr_number)
            item = dict(items[idx])
            if (item.get("status") != "submitted" or not item.get("attempt_id")
                    or not item.get("submitted_at") or not item.get("sent")):
                raise QueueStateError("sync-tracker requires a durable submitted attempt")
            if dry_run:
                sync_item = item
            else:
                item.update({"tracker_sync": "syncing",
                             "tracker_sync_id": sync_id,
                             "tracker_sync_started_at": now_utc()})
                items[idx] = item
                _write_queue_unlocked(queue_path, items)
                sync_item = dict(item)
    except (QueueStateError, OSError) as exc:
        log("queue: tracker sync could not start: " + str(exc))
        return 1
    if dry_run:
        result = write_tracker_row(repo, sync_item, sync_item["submitted_at"],
                                   sync_item["sent"], sync_item.get("route", ""),
                                   dry_run=True, log=log)
        return 0 if result else 1

    # Tracker fetch/push is deliberately outside the queue lock.
    try:
        result = write_tracker_row(repo, sync_item, sync_item["submitted_at"],
                                   sync_item["sent"], sync_item.get("route", ""),
                                   log=log)
    except Exception as exc:  # noqa: BLE001 - persisted submission stays terminal
        log("queue: tracker sync interrupted: " + type(exc).__name__)
        result = ""
    try:
        with queue_lock(queue_path):
            latest = load_queue(queue_path)
            idx = _unique_item_index(latest, pr_number)
            current = dict(latest[idx])
            if (current.get("status") != "submitted"
                    or current.get("attempt_id") != sync_item.get("attempt_id")
                    or current.get("tracker_sync_id") != sync_id):
                log("queue: tracker result is stale; newer bookkeeping was preserved")
                return 1 if not result or result == TRACKER_CONFLICT else 0
            if result and result != TRACKER_CONFLICT:
                current["tracker_sync"] = "synced"
                current["tracker_synced_at"] = now_utc()
                current.pop("tracker_sync_error", None)
                if result != "present":
                    current["tracker_commit"] = result
            else:
                current["tracker_sync"] = "error"
                current["tracker_sync_error"] = (
                    "tracker_identity_conflict" if result == TRACKER_CONFLICT
                    else "tracker_write_failed")
                current["tracker_sync_failed_at"] = now_utc()
            latest[idx] = current
            _write_queue_unlocked(queue_path, latest)
    except (QueueStateError, OSError) as exc:
        log("queue: tracker result could not be persisted: " + str(exc))
        return 1
    return 0 if result and result != TRACKER_CONFLICT else 1


def cmd_sync_tracker(args, log):
    return sync_tracker_attempt(args.queue, args.pr, args.repo, log,
                                dry_run=args.dry_run)


# ---------------------------------------------------------------- parser
def add_common(ap):
    ap.add_argument("--queue", default=QUEUE_FILE,
                    help="queue store (env JOBSEARCH_QUEUE; default under user state directory)")


def build_parser():
    ap = argparse.ArgumentParser(
        description="Application queue: merged tailoring PRs -> drain contract.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    e = sub.add_parser("enqueue", help="append newly merged app/* tailoring PRs")
    add_common(e)
    e.add_argument("--repo", default=REPO_DIR,
                   help="resume clone (env JOBSEARCH_REPO)")
    e.add_argument("--dry-run", action="store_true", help="print, write nothing")
    e.add_argument("--json", action="store_true", help="emit added/skipped as JSON")
    e.add_argument("--limit", type=int, default=0, help="add at most N items (0 = all)")
    e.add_argument("--include-submitted", action="store_true",
                   help="also enqueue apps already recorded as submitted")
    e.add_argument("--prs", help="preview only, requires --dry-run: JSON list of PRs (gh shape)")
    e.add_argument("--tracker", help="preview only, requires --dry-run: applications.yaml text")

    l = sub.add_parser("list", help="list queue items (oldest first)")
    add_common(l)
    l.add_argument("--json", action="store_true")
    l.add_argument("--status", choices=STATUSES, help="filter by status")

    n = sub.add_parser("next", help="print the oldest queued item")
    add_common(n)
    n.add_argument("--json", action="store_true")

    c = sub.add_parser("claim", help="durably authorize one eligible queue attempt")
    add_common(c)
    c.add_argument("--repo", default=REPO_DIR,
                   help="resume clone with current postings, profile, and tracker")
    c.add_argument("--profile", help="exact profile.yaml path (default: <repo>/data/profile.yaml)")
    c.add_argument("--pr", type=int, required=True, help="PR number of the queued item")
    c.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="update an item (drain bookkeeping)")
    add_common(s)
    s.add_argument("--repo", default=REPO_DIR,
                   help="resume clone for the tracker write-back "
                        "(env JOBSEARCH_REPO)")
    s.add_argument("--pr", type=int, required=True, help="PR number of the item")
    s.add_argument("--status", required=True,
                   choices=["queued", "in_progress", "submitted", "failed"],
                   help="only submitted confirmation or terminal pre-claim failure")
    s.add_argument("--attempt-id", help="ID returned by a successful durable claim")
    s.add_argument("--date", help="submission date (YYYY-MM-DD); required for submitted")
    s.add_argument("--sent", help="exactly what was sent, required for submitted")
    s.add_argument("--confirmation-evidence",
                    help="observed employer confirmation, required for submitted")
    s.add_argument("--route", help="submission route (site/ATS or LinkedIn Easy Apply)")
    s.add_argument("--notes", help="free-form note (required for failed)")
    s.add_argument("--no-tracker", action="store_true",
                   help="skip the automatic applications.yaml write-back "
                        "(e.g. when the row was appended by hand)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="deprecated; cannot override attempt or terminal-state gates")

    prep = sub.add_parser("prepare-snapshot",
                          help="retain selected PDF and answers for the active attempt")
    add_common(prep)
    prep.add_argument("--repo", default=REPO_DIR,
                      help="private resume repo used to verify available source commits")
    prep.add_argument("--pr", type=int, required=True)
    prep.add_argument("--attempt-id", required=True)
    prep.add_argument("--pdf", required=True, help="exact selected resume PDF")
    prep.add_argument("--answers", required=True, help="exact selected answers JSON")
    prep.add_argument("--operator-verification", required=True,
                      help="what you checked about these exact materials")

    verify = sub.add_parser("verify-snapshot",
                            help="verify retained and currently selected materials before Submit")
    add_common(verify)
    verify.add_argument("--repo", default=REPO_DIR,
                        help="private resume repo, excluded from snapshot storage")
    verify.add_argument("--pr", type=int, required=True)
    verify.add_argument("--attempt-id", required=True)
    verify.add_argument("--pdf", required=True, help="same selected resume PDF")
    verify.add_argument("--answers", required=True, help="same selected answers JSON")

    finalize = sub.add_parser("finalize-snapshot",
                              help="repair provenance for a recorded submission; never submits")
    add_common(finalize)
    finalize.add_argument("--repo", default=REPO_DIR,
                          help="private resume repo, excluded from snapshot storage")
    finalize.add_argument("--pr", type=int, required=True)
    finalize.add_argument("--attempt-id", required=True)

    r = sub.add_parser("release-stale",
                       help="move abandoned claims to outcome_unknown for reconciliation")
    add_common(r)
    r.add_argument("--dry-run", action="store_true")

    rec = sub.add_parser("reconcile", help="resolve one outcome_unknown attempt from evidence")
    add_common(rec)
    rec.add_argument("--repo", default=REPO_DIR, help="resume clone for tracker sync")
    rec.add_argument("--pr", type=int, required=True)
    rec.add_argument("--attempt-id", required=True)
    rec.add_argument("--result", required=True, choices=["retry", "submitted"])
    rec.add_argument("--evidence-source", required=True,
                     help="specific portal/history/email source inspected")
    rec.add_argument("--evidence-observation", required=True,
                     help="concrete observed outcome, not a review note")
    rec.add_argument("--date", help="submission date for confirmed submission")
    rec.add_argument("--sent", help="exactly what was sent for confirmed submission")
    rec.add_argument("--confirmation-evidence",
                     help="observed employer confirmation for submitted result")
    rec.add_argument("--route", help="submission route")
    rec.add_argument("--no-tracker", action="store_true",
                     help="leave submitted tracker bookkeeping pending")

    sync = sub.add_parser("sync-tracker", help="repair tracker bookkeeping for a submitted attempt")
    add_common(sync)
    sync.add_argument("--repo", default=REPO_DIR, help="resume clone for tracker write-back")
    sync.add_argument("--pr", type=int, required=True)
    sync.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)  # noqa: E731
    try:
        if args.cmd == "enqueue":
            return cmd_enqueue(args, log)
        if args.cmd == "list":
            return cmd_list(args, log)
        if args.cmd == "next":
            return cmd_next(args, log)
        if args.cmd == "claim":
            import tailor
            args.skipped = tailor.SKIPPED_FILE
            args.gates = tailor.GATE_FILE
            return cmd_claim(args, log)
        if args.cmd == "status":
            return cmd_status(args, log)
        if args.cmd == "prepare-snapshot":
            return cmd_prepare_snapshot(args, log)
        if args.cmd == "verify-snapshot":
            return cmd_verify_snapshot(args, log)
        if args.cmd == "finalize-snapshot":
            return cmd_finalize_snapshot(args, log)
        if args.cmd == "release-stale":
            return cmd_release_stale(args, log)
        if args.cmd == "reconcile":
            return cmd_reconcile(args, log)
        if args.cmd == "sync-tracker":
            return cmd_sync_tracker(args, log)
        build_parser().print_help()
        return 2
    except (QueueStateError, OSError, UnicodeError, ValueError) as exc:
        log("queue: operation failed closed: " + str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
