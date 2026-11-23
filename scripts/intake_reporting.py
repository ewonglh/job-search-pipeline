"""Bounded, sanitized private intake run history shared by producers and reports."""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from config import default_runtime_path, path_setting

LANES = {"discovery", "enqueue"}
OUTCOMES = {"healthy", "healthy_empty", "partial_failure", "total_failure", "failed"}
COUNTS = {"processed", "accepted", "added", "admitted", "excluded", "rejected",
          "duplicates", "limited", "pending_metadata", "observed", "owner_merged",
          "closed_history", "active_work"}
REASONS = {"no_postings", "filtered_out", "already_stored", "limit_reached",
           "no_merged_prs", "no_owner_merges", "no_eligible_prs", "already_queued",
           "already_submitted", "already_active", "not_application_pr", "not_tailoring_pr",
           "identity_review", "source_unavailable", "challenge_page", "parse_unrecognized",
           "parse_failed", "metadata_unavailable", "queue_unavailable", "tracker_unavailable",
           "unknown"}
FETCH = {"ok", "failed", "unknown"}
PARSE = {"ok", "empty", "challenge", "failed", "unrecognized", "not_attempted"}
KINDS = {"internsg", "company", "board", "import"}
HISTORY_LIMIT = 40
MAX_SOURCES_PER_RUN = 500
MAX_HISTORY_BYTES = 5 * 1024 * 1024
CHALLENGE_PAGE_RE = re.compile(
    r"cf-chl-|challenge-platform|verify (?:that )?you are human|just a moment|"
    r"captcha|access denied|checking your browser", re.I)


def history_path(lane, directory=None):
    if lane not in LANES:
        raise ValueError("unsupported intake lane")
    root = (os.path.abspath(os.path.expanduser(directory)) if directory is not None else
            path_setting("JOBSEARCH_INTAKE_DIR", default_runtime_path("intake")))
    return os.path.join(root, lane + ".json")


def source_id(kind, key):
    return kind + ":" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]


def is_challenge_response(body):
    """Recognize common anti-bot challenge pages without retaining their text."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    return isinstance(body, str) and bool(CHALLENGE_PAGE_RE.search(body))


def _counts(value, allowed):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if key in allowed and type(number) is int and number >= 0}


def sanitize_summary(value):
    """Drop all text, URLs, paths and unknown fields at the producer boundary."""
    if not isinstance(value, dict) or value.get("outcome") not in OUTCOMES:
        raise ValueError("invalid intake summary")
    result = {"outcome": value["outcome"], "counts": _counts(value.get("counts"), COUNTS),
              "reasons": _counts(value.get("reasons"), REASONS)}
    raw_sources = value.get("sources", [])
    if not isinstance(raw_sources, list) or len(raw_sources) > MAX_SOURCES_PER_RUN:
        raise ValueError("invalid intake source coverage")
    sources = []
    for row in raw_sources:
        if not isinstance(row, dict) or row.get("kind") not in KINDS:
            continue
        kind, identity = row["kind"], row.get("source_id", "")
        if (not isinstance(identity, str) or not identity.startswith(kind + ":")
                or len(identity) != len(kind) + 17
                or any(char not in "0123456789abcdef" for char in identity[len(kind) + 1:])):
            continue
        if row.get("fetch") not in FETCH or row.get("parse") not in PARSE:
            continue
        sources.append({"kind": kind, "source_id": identity, "fetch": row["fetch"],
                        "parse": row["parse"], **_counts(row, {"processed", "accepted"})})
    if sources:
        result["sources"] = sources
    return result


def read_history(lane, directory=None):
    path = history_path(lane, directory)
    if os.path.getsize(path) > MAX_HISTORY_BYTES:
        raise ValueError("intake history exceeds the supported size")
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("invalid intake history")
    rows = document.get("runs")
    if not isinstance(rows, list) or len(rows) > HISTORY_LIMIT:
        raise ValueError("invalid intake history")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid intake run")
        stamp = row.get("recorded_at")
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else None
        if parsed is None or parsed.tzinfo is None:
            raise ValueError("invalid intake time")
        result.append({"recorded_at": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       **sanitize_summary(row)})
    return result


def record_run(lane, summary):
    """Append one sanitized run. Call after durable work; do not call in dry-run."""
    path = history_path(lane)
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    # Reuse the existing cross-platform writer lock, without importing queue.py.
    from email_ingest import state_lock
    with state_lock(path):
        try:
            rows = read_history(lane)
        except FileNotFoundError:
            rows = []
        row = {"recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
               **sanitize_summary(summary)}
        rows = (rows + [row])[-HISTORY_LIMIT:]
        descriptor, temporary = tempfile.mkstemp(dir=parent, prefix=".intake-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "runs": rows}, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return row
