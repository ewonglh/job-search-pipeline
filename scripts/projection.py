#!/usr/bin/env python3
"""Private SQLite projection for cached, read-only pipeline reports.

Collection is explicit: ``projection.py collect`` reads configured local
sources and publishes one sanitized snapshot. ``read`` and ``report`` open the
database read-only and never inspect source files or perform network/model I/O.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, quote, urlsplit

from config import default_repo_dir, default_runtime_path, path_setting

import report_snapshot


SCHEMA_VERSION = 1
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000
MAX_SOURCE_RECORDS = 200000
SNAPSHOT_HISTORY = 8
SOURCES = ("tracker", "queue", "events", "ingestion", "snapshots",
           "reviews", "preflight")
HASH40_RE = re.compile(r"^[0-9a-f]{40}$")
HASH64_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TEXT_FIELDS = {
    "company": 120, "role": 120, "source": 80, "status": 80,
    "stage": 80, "tracker_sync": 80, "snapshot_provenance": 80,
    "attempt_state": 80, "blocker_code": 80, "attempt_id": 120,
}
TRACKER_FIELDS = ("company", "role", "link", "pr", "pr_number", "source",
                  "status", "stage", "applied_date", "submitted_at")
QUEUE_FIELDS = ("company", "role", "link", "pr", "pr_number", "source",
                "status", "submitted_at", "claimed_at", "attempt_id",
                "tracker_sync", "snapshot_id", "prepared_snapshot_id",
                "snapshot_provenance", "attempt_state", "blocker_code")
TRACKER_STATUS_VALUES = {
    "found", "to_apply", "todo", "not_applied", "tailored", "tailor", "ready",
    "applied", "submitted", "callback", "recruiter", "info_requested", "rfi",
    "screening", "screen", "oa", "assessment", "online_assessment", "hackerrank",
    "codility", "test", "challenge", "interview", "interviewing", "interviewed",
    "offer", "offered", "accepted", "signed", "rejected", "rejection", "withdrawn",
    "declined", "found", "applied", "callback", "oa", "interview", "accepted",
    "rejected", "unknown", "",
}
QUEUE_ENUMS = {
    "tracker_sync": {"pending", "syncing", "synced", "error"},
    "snapshot_provenance": {"pending", "prepared", "verified", "complete",
                             "incomplete", "unknown"},
    "attempt_state": {"authorized", "submitted", "outcome_unknown", "failed"},
}
SOURCE_FRESHNESS = dict(report_snapshot.SOURCE_FRESHNESS_SECONDS)
SOURCE_FRESHNESS.update({"reviews": 86400, "preflight": 86400})


class ProjectionError(Exception):
    """A safe, user-facing projection error with no source content attached."""


class _InvalidSource(Exception):
    def __init__(self, count=1):
        self.count = count


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("collection time must include a timezone")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value.strip() else None


def _safe_text(value, limit):
    if not isinstance(value, str):
        raise _InvalidSource()
    compact = " ".join(value.split())
    if len(compact) > limit or any(ord(char) < 32 for char in compact):
        raise _InvalidSource()
    return compact or None


def _safe_url(value):
    text = _safe_text(value, 2048)
    if text is None:
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        raise _InvalidSource()
    secret_params = {"token", "access_token", "auth", "authorization", "password",
                     "secret", "email", "session", "sessionid", "sig", "signature",
                     "api_key", "apikey"}
    query_keys = {key.lower() for key, _value in parse_qsl(parts.query)}
    if (parts.scheme.lower() not in ("http", "https") or not host
            or parts.username or parts.password or secret_params.intersection(query_keys)):
        raise _InvalidSource()
    return text


def _strict_rows(rows, fields, *, queue_statuses=None, tracker_statuses=None):
    if not isinstance(rows, list) or len(rows) > MAX_SOURCE_RECORDS:
        raise _InvalidSource()
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise _InvalidSource()
        clean = {}
        for key in fields:
            if key not in row or row[key] is None:
                continue
            value = row[key]
            if key in TEXT_FIELDS:
                value = _safe_text(value, TEXT_FIELDS[key])
                if value and key == "attempt_id" and not re.fullmatch(
                        r"[A-Za-z0-9._:-]{1,120}", value):
                    raise _InvalidSource()
                if value and key == "blocker_code" and not CODE_RE.fullmatch(value.lower()):
                    raise _InvalidSource()
                if value and key in QUEUE_ENUMS and value not in QUEUE_ENUMS[key]:
                    raise _InvalidSource()
                if value is not None and key in ("status", "stage"):
                    allowed = (queue_statuses if queue_statuses and key == "status"
                               else tracker_statuses or TRACKER_STATUS_VALUES)
                    if value.lower() not in allowed and value not in allowed:
                        raise _InvalidSource()
            elif key == "link":
                value = _safe_url(value)
            elif key in ("pr", "pr_number"):
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise _InvalidSource()
                if isinstance(value, str):
                    value = _safe_text(value, 2048)
                    if value and "://" in value:
                        value = _safe_url(value)
                    elif value and not value.isdigit():
                        raise _InvalidSource()
            elif key in ("applied_date",):
                if not isinstance(value, str) or not _day(value):
                    raise _InvalidSource()
                value = _day(value)
            elif key in ("submitted_at", "claimed_at"):
                if not isinstance(value, str):
                    raise _InvalidSource()
                value = _timestamp(value) or _day(value)
                if value is None:
                    raise _InvalidSource()
            elif key in ("snapshot_id", "prepared_snapshot_id"):
                if not isinstance(value, str) or not HASH64_RE.fullmatch(value):
                    raise _InvalidSource()
            if value is not None:
                clean[key] = value
        if not any(clean.get(key) for key in ("company", "role", "link", "pr", "pr_number")):
            raise _InvalidSource()
        result.append(clean)
    return result


def _unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate key")
        output[key] = value
    return output


def _read_json(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source, object_pairs_hook=_unique_object)


def _source_error(path, error):
    if isinstance(error, FileNotFoundError):
        return {"records": None, "observed_at": None, "error_code": "source_missing",
                "quarantined_count": 0}
    if isinstance(error, PermissionError):
        return {"records": None, "observed_at": None, "error_code": "source_unreadable",
                "quarantined_count": 0}
    if isinstance(error, (UnicodeError, ValueError, TypeError, _InvalidSource)):
        return {"records": None, "observed_at": None, "error_code": "source_invalid",
                "quarantined_count": getattr(error, "count", 1)}
    return {"records": None, "observed_at": None, "error_code": "source_unavailable",
            "quarantined_count": 0}


def _read_tracker(path):
    try:
        import summary
        with open(path, "r", encoding="utf-8") as source:
            text = source.read()
        parsed = summary.parse_yaml_list(text)
        meaningful = [line.strip() for line in text.splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
        if not parsed and "[]" not in meaningful:
            raise _InvalidSource()
        statuses = set(summary.STATUS_ALIASES) | {str(value).lower()
                                                 for value in summary.STATUS_ALIASES.values()}
        return {"records": _strict_rows(parsed, TRACKER_FIELDS,
                                        tracker_statuses=statuses | TRACKER_STATUS_VALUES),
                "observed_at": None,
                "error_code": None, "quarantined_count": 0}
    except (OSError, UnicodeError, ValueError, TypeError, _InvalidSource) as exc:
        return _source_error(path, exc)


def _read_queue(path):
    try:
        import queue as queue_module
        with open(path, "r", encoding="utf-8") as source:
            parsed = queue_module.parse_queue(source.read())
        return {"records": _strict_rows(parsed, QUEUE_FIELDS,
                                        queue_statuses=set(queue_module.STATUSES)),
                "observed_at": None, "error_code": None, "quarantined_count": 0}
    except (OSError, UnicodeError, ValueError, TypeError, _InvalidSource) as exc:
        return _source_error(path, exc)


EVENT_FIELDS = ("event_id", "event_type", "application_id", "occurred_at",
                "verification", "origin")


def _read_events(path):
    try:
        import recruitment_evidence
        document = _read_json(path)
        if (not isinstance(document, dict) or document.get("schema") != 1
                or not isinstance(document.get("coverage"), dict)
                or not isinstance(document.get("events"), list)):
            raise _InvalidSource()
        if len(document["events"]) > MAX_SOURCE_RECORDS:
            raise _InvalidSource()
        effective, quarantined, diagnostics = recruitment_evidence.effective_events(document)
        coverage = document["coverage"]
        safe_coverage = {}
        for field in ("complete", "transport_complete"):
            if field in coverage:
                if type(coverage[field]) is not bool:
                    raise _InvalidSource()
                safe_coverage[field] = coverage[field]
        for field in ("observed_at", "last_scan_at"):
            if field in coverage and coverage[field] is not None:
                stamp = _timestamp(coverage[field])
                if stamp is None:
                    raise _InvalidSource()
                safe_coverage[field] = stamp
        for field in ("since", "until"):
            if field in coverage and coverage[field] is not None:
                day = _day(coverage[field])
                if day is None:
                    raise _InvalidSource()
                safe_coverage[field] = day
        events = []
        allowed_origins = {"human", "automated", "unknown"}
        allowed_verification = {"verified", "review_required", "unknown"}
        for event in effective:
            if not isinstance(event, dict):
                raise _InvalidSource()
            clean = {}
            for field in EVENT_FIELDS:
                if field not in event or event[field] is None:
                    continue
                value = event[field]
                if field == "event_id":
                    value = _safe_text(value, 256)
                    if value:
                        value = "event:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
                elif field == "application_id":
                    value = _safe_text(value, 2048)
                    if value and value.startswith("link:"):
                        value = "link:" + _safe_url(value[len("link:"):])
                    elif value and value.startswith("pr:"):
                        if not re.fullmatch(r"pr:[1-9][0-9]*", value):
                            raise _InvalidSource()
                    elif value and value.startswith("identity:"):
                        if "|" not in value[len("identity:"):]:
                            raise _InvalidSource()
                    elif value:
                        raise _InvalidSource()
                elif field == "event_type":
                    if value not in recruitment_evidence.EVENT_TYPES:
                        raise _InvalidSource()
                elif field == "occurred_at":
                    value = _timestamp(value)
                    if value is None:
                        raise _InvalidSource()
                elif field == "verification" and value not in allowed_verification:
                    raise _InvalidSource()
                elif field == "origin" and value not in allowed_origins:
                    raise _InvalidSource()
                clean[field] = value
            events.append({"kind": "event", "value": clean,
                           "quarantined": event.get("event_id") in quarantined})
        observed = (_timestamp(safe_coverage.get("observed_at"))
                    or _timestamp(safe_coverage.get("last_scan_at")))
        records = [{"kind": "coverage", "value": safe_coverage},
                   {"kind": "diagnostics", "value": diagnostics,
                    "quarantined_event_count": len(quarantined)}]
        records.extend(events)
        return {"records": records, "record_count": len(document["events"]),
                "observed_at": observed, "error_code": None,
                "quarantined_count": 0}
    except (OSError, UnicodeError, ValueError, TypeError, _InvalidSource) as exc:
        return _source_error(path, exc)


def _read_ingestion(path):
    try:
        document = _read_json(path)
        if (not isinstance(document, dict) or document.get("schema") != 1
                or not isinstance(document.get("scopes"), dict)
                or len(document["scopes"]) > MAX_SOURCE_RECORDS):
            raise _InvalidSource()
        records, observed = [], None
        for scope in document["scopes"].values():
            if not isinstance(scope, dict):
                raise _InvalidSource()
            value = scope.get("last_scan_at")
            if value is not None:
                value = _timestamp(value)
                if value is None:
                    raise _InvalidSource()
                observed = max(observed, value) if observed else value
            records.append({"kind": "scope", "value": {"last_scan_at": value}})
        return {"records": records, "record_count": len(records),
                "observed_at": observed, "error_code": None,
                "quarantined_count": 0}
    except (OSError, UnicodeError, ValueError, TypeError, _InvalidSource) as exc:
        return _source_error(path, exc)


def _read_snapshots(root):
    if not os.path.isdir(root):
        return {"records": None, "observed_at": None, "error_code": "source_missing",
                "quarantined_count": 0}
    return {"records": [{"present": True}], "observed_at": None,
            "error_code": None, "quarantined_count": 0}


def _read_reviews(path):
    try:
        import review_contract
        document = _read_json(path)
        reviewed = document.get("reviewed") if isinstance(document, dict) else None
        if not isinstance(reviewed, dict) or len(reviewed) > MAX_SOURCE_RECORDS:
            raise _InvalidSource()
        grouped, unknown_heads, timestamps = {}, 0, []
        for entry in reviewed.values():
            if not isinstance(entry, dict):
                raise _InvalidSource()
            raw_head = entry.get("review_head_sha")
            head = raw_head if isinstance(raw_head, str) and HASH40_RE.fullmatch(raw_head) else None
            if head is None:
                unknown_heads += 1
                continue
            provenance = entry.get("provenance")
            valid_provenance, _ = review_contract.valid_provenance(provenance, head)
            verdict = entry.get("verdict")
            accepted = False
            try:
                accepted = review_contract.approval_valid(entry, head)
            except (TypeError, ValueError, KeyError, AttributeError):
                accepted = False
            if accepted:
                model_state = "accepted"
            elif verdict == "reject":
                model_state = "not_accepted"
            elif verdict == "approve":
                model_state = "unknown"
            else:
                model_state = "unknown"
            provenance_state = "verified" if valid_provenance else (
                "unknown" if not isinstance(provenance, dict) else "invalid")
            checked = None
            audit = entry.get("audit")
            if isinstance(audit, dict):
                checked = _timestamp(audit.get("checked_at"))
            if checked:
                timestamps.append(checked)
            grouped.setdefault(head, []).append({
                "head": head, "model_acceptance": model_state,
                "provenance_status": provenance_state,
                "checked_at": checked,
            })
        records = []
        for head, entries in sorted(grouped.items()):
            # First-pass ordering is only known when every candidate has an
            # explicit checked_at. A tie with differing outcomes stays unknown.
            if len(entries) == 1:
                selected = entries[0]
                model_state = selected["model_acceptance"]
                provenance_state = selected["provenance_status"]
            elif all(item["checked_at"] for item in entries):
                earliest = min(item["checked_at"] for item in entries)
                first = [item for item in entries if item["checked_at"] == earliest]
                states = {item["model_acceptance"] for item in first}
                model_state = states.pop() if len(states) == 1 else "unknown"
                provenance_state = ("verified" if all(item["provenance_status"] == "verified"
                                                       for item in first) else "unknown")
            else:
                model_state, provenance_state = "unknown", "unknown"
            records.append({"artifact_head": head,
                            "model_acceptance": model_state,
                            "owner_acceptance": "unknown",
                            "provenance_status": provenance_state,
                            "rework_codes": []})
        return {"records": records, "record_count": len(reviewed),
                "observed_at": max(timestamps) if timestamps else None,
                "unknown_head_count": unknown_heads, "error_code": None,
                "quarantined_count": 0}
    except (OSError, UnicodeError, ValueError, TypeError, _InvalidSource) as exc:
        return _source_error(path, exc)


PREFLIGHT_CATEGORIES = {"content", "eligibility", "closure", "staleness",
                        "infrastructure", "build_layout"}
PREFLIGHT_CODES = {
    "stale_inputs", "artifact_content_mismatch", "artifact_head_mismatch",
    "input_provenance_missing", "eligibility_record_mismatch", "eligibility_conflict",
    "eligibility_unknown", "work_item_invalid", "input_unavailable",
    "artifact_unavailable", "current_jd_unavailable", "posting_guard_hostile",
    "posting_closed", "input_read_failed", "artifact_read_failed",
    "master_decode_failed", "layout_checker_unavailable", "layout_checker_failed",
    "artifact_head_unavailable", "template_unsupported", "unmatched_entry",
    "bullet_floor", "team_attribution", "unsupported_number", "chronology",
    "layout_tool_failed", "layout_artifact_unreadable", "layout_page_count",
}
PREFLIGHT_HASH_KEYS = {"master_sha256", "profile_sha256", "jd_sha256",
                       "role_title_sha256", "combined_text_sha256"}


def _owner_acceptance(owner, report):
    if owner is None:
        return "unknown"
    if not isinstance(owner, dict):
        return "unknown"
    accepted_at = _timestamp(owner.get("accepted_at"))
    accepted_by = owner.get("accepted_by")
    if (type(owner.get("schema_version")) is not int or owner.get("schema_version") != 1
            or owner.get("record_type") != "owner_accepted_preflight"
            or owner.get("accepted") is not True
            or not isinstance(accepted_by, str) or not accepted_by.strip()
            or accepted_at is None):
        return "unknown"
    complete_hashes = (isinstance(report["input_hashes"], dict)
                       and set(report["input_hashes"]) == PREFLIGHT_HASH_KEYS)
    exact = (complete_hashes and bool(HASH40_RE.fullmatch(report["head"] or ""))
             and bool(HASH64_RE.fullmatch(report["artifact_sha256"] or ""))
             and report["status"] == "exact_prior_acceptance"
             and report["reuse_status"] == "exact_prior_acceptance"
             and not report["findings"]
             and owner.get("head") == report["head"]
             and owner.get("artifact_sha256") == report["artifact_sha256"]
             and owner.get("input_hashes") == report["input_hashes"])
    return "accepted" if exact else "not_accepted"


def _read_preflight(path):
    try:
        document = _read_json(path)
        rows = document.get("records") if isinstance(document, dict) else None
        if (not isinstance(document, dict) or document.get("schema") != 1
                or not isinstance(rows, list) or len(rows) > MAX_SOURCE_RECORDS):
            raise _InvalidSource()
        grouped, unknown_heads, observed = {}, 0, None
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("report"), dict):
                raise _InvalidSource()
            report = row["report"]
            if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
                raise _InvalidSource()
            head = report.get("head")
            head = head if isinstance(head, str) and HASH40_RE.fullmatch(head) else None
            artifact = report.get("artifact_sha256")
            if artifact and (not isinstance(artifact, str) or not HASH64_RE.fullmatch(artifact)):
                raise _InvalidSource()
            hashes = report.get("input_hashes", {})
            if not isinstance(hashes, dict) or any(
                    key not in PREFLIGHT_HASH_KEYS or not isinstance(value, str)
                    or not HASH64_RE.fullmatch(value) for key, value in hashes.items()):
                raise _InvalidSource()
            status = report.get("status")
            reuse = report.get("reuse_status")
            if status not in {"exact_prior_acceptance", "review_required"}:
                raise _InvalidSource()
            if reuse not in {"exact_prior_acceptance", "review_required"}:
                raise _InvalidSource()
            raw_findings = report.get("findings")
            if not isinstance(raw_findings, list) or len(raw_findings) > 1000:
                raise _InvalidSource()
            codes = set()
            for finding in raw_findings:
                if not isinstance(finding, dict):
                    raise _InvalidSource()
                category, code, message = (finding.get("category"), finding.get("code"),
                                           finding.get("message"))
                if category not in PREFLIGHT_CATEGORIES:
                    raise _InvalidSource()
                if not isinstance(code, str) or code not in PREFLIGHT_CODES:
                    raise _InvalidSource()
                if not isinstance(message, str):
                    raise _InvalidSource()
                codes.add(code)
            if head is None:
                unknown_heads += 1
                continue
            owner_state = _owner_acceptance(row.get("owner_record"), {
                "status": status, "reuse_status": reuse, "findings": raw_findings,
                "head": head, "artifact_sha256": artifact, "input_hashes": hashes,
            })
            grouped.setdefault(head, []).append({"owner_acceptance": owner_state,
                                                  "provenance_status": "verified"
                                                  if set(hashes) == PREFLIGHT_HASH_KEYS else "unknown",
                                                  "rework_codes": sorted(codes)})
        records = []
        for head, entries in sorted(grouped.items()):
            if len(entries) == 1:
                owner_state = entries[0]["owner_acceptance"]
                provenance_state = entries[0]["provenance_status"]
            else:
                owner_states = {item["owner_acceptance"] for item in entries}
                owner_state = owner_states.pop() if len(owner_states) == 1 else "unknown"
                provenance_state = "unknown"
            records.append({"artifact_head": head,
                            "model_acceptance": "unknown",
                            "owner_acceptance": owner_state,
                            "provenance_status": provenance_state,
                            "rework_codes": sorted({code for item in entries
                                                    for code in item["rework_codes"]})})
        return {"records": records, "record_count": len(rows), "observed_at": observed,
                "unknown_head_count": unknown_heads,
                "error_code": None, "quarantined_count": 0}
    except (OSError, UnicodeError, ValueError, TypeError, _InvalidSource) as exc:
        return _source_error(path, exc)


def _source_payload(name, value):
    records = value["records"]
    if records is None:
        return value
    if len(records) > MAX_SOURCE_RECORDS:
        return _source_error("", _InvalidSource())
    return value


def _db_default():
    private_dir = default_runtime_path("projection")
    return path_setting("JOBSEARCH_PROJECTION_DB",
                        os.path.join(private_dir, "index.sqlite3"))


def _is_inside_checkout(path):
    checkout = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    target = os.path.realpath(path)
    try:
        return os.path.commonpath([os.path.normcase(checkout), os.path.normcase(target)]) == os.path.normcase(checkout)
    except ValueError:
        return False


def _connect_writer(path):
    absolute = os.path.abspath(os.path.expanduser(path))
    if _is_inside_checkout(absolute):
        raise ProjectionError("projection database must be outside the code checkout")
    parent = os.path.dirname(absolute)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.name != "nt":
        try:
            permissions = stat.S_IMODE(os.stat(parent).st_mode)
        except OSError as exc:
            raise ProjectionError("projection directory permissions could not be checked") from exc
        if permissions & 0o077:
            raise ProjectionError("projection database directory must be private (mode 0700)")
    connection = sqlite3.connect(absolute, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    _ensure_schema(connection)
    if os.name != "nt":
        try:
            os.chmod(absolute, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = absolute + suffix
                if os.path.exists(sidecar):
                    os.chmod(sidecar, 0o600)
        except OSError:
            pass
    return connection


def _ensure_schema(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT PRIMARY KEY,
            active_generation TEXT,
            status TEXT NOT NULL,
            observed_at TEXT,
            collected_at TEXT,
            last_success_at TEXT,
            freshness_seconds INTEGER NOT NULL,
            error_code TEXT,
            record_count INTEGER NOT NULL DEFAULT 0,
            quarantined_count INTEGER NOT NULL DEFAULT 0,
            error_quarantined_count INTEGER NOT NULL DEFAULT 0,
            empty INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS source_generations (
            source TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            state TEXT NOT NULL,
            cursor INTEGER NOT NULL,
            total INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY(source, generation_id)
        );
        CREATE TABLE IF NOT EXISTS source_records (
            source TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            record_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(source, generation_id, position),
            UNIQUE(source, generation_id, record_id),
            FOREIGN KEY(source, generation_id)
              REFERENCES source_generations(source, generation_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
    """)
    existing = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if existing and existing[0] != str(SCHEMA_VERSION):
        raise ProjectionError("unsupported projection database schema")
    connection.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
                       (str(SCHEMA_VERSION),))
    for source in SOURCES:
        connection.execute("INSERT OR IGNORE INTO source_state(source,status,freshness_seconds) VALUES(?,?,?)",
                           (source, "unavailable", SOURCE_FRESHNESS[source]))


def _connect_reader(path):
    absolute = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(absolute):
        raise ProjectionError("cached projection is unavailable; run projection.py collect")
    uri = "file:" + quote(absolute.replace("\\", "/"), safe="/:" ) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or version[0] != str(SCHEMA_VERSION):
            connection.close()
            raise ProjectionError("unsupported projection database schema")
        return connection
    except sqlite3.Error as exc:
        raise ProjectionError("cached projection could not be read") from exc


def _record_id(source, position, payload, occurrence):
    return hashlib.sha256((source + "\0" + _canonical(payload) + "\0" +
                           str(occurrence)).encode("utf-8")).hexdigest()


def _source_success(connection, source, generation, value, collected_at):
    observed = value.get("observed_at")
    status = "stale" if observed is None else "fresh"
    error = "observation_time_unknown" if observed is None else None
    if observed is not None:
        age = max(0, (datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
                      - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds())
        if age > SOURCE_FRESHNESS[source]:
            status = "stale"
    count = value.get("record_count", len(value["records"]))
    connection.execute("""UPDATE source_state SET active_generation=?,status=?,observed_at=?,
        collected_at=?,last_success_at=?,freshness_seconds=?,error_code=?,record_count=?,
        quarantined_count=?,error_quarantined_count=0,empty=? WHERE source=?""",
        (generation, status, observed, collected_at, collected_at, SOURCE_FRESHNESS[source],
         error, count, value.get("unknown_head_count", value.get("quarantined_count", 0)),
         1 if count == 0 and not value.get("unknown_head_count") else 0, source))


def _source_failure(connection, source, value, collected_at):
    old = connection.execute("SELECT active_generation,last_success_at,observed_at,record_count,quarantined_count FROM source_state WHERE source=?",
                             (source,)).fetchone()
    has_last_good = bool(old and old["active_generation"])
    connection.execute("""UPDATE source_state SET status=?,collected_at=?,error_code=?,
        error_quarantined_count=?,quarantined_count=?,record_count=?,empty=? WHERE source=?""",
        ("stale" if has_last_good else "unavailable", collected_at,
         value.get("error_code") or "source_unavailable",
         value.get("quarantined_count", 0),
         old["quarantined_count"] if has_last_good else value.get("quarantined_count", 0),
         old["record_count"] if has_last_good else 0,
         1 if has_last_good and old["record_count"] == 0
         and old["quarantined_count"] == 0 else 0, source))


def _put_progress(connection, progress):
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('collection_progress',?)",
                       (_canonical(progress),))


def _replace_source(connection, source, value, collected_at, batch_size,
                    batch_limit=None, batches_already=0):
    if value["records"] is None:
        connection.execute("BEGIN IMMEDIATE")
        _source_failure(connection, source, value, collected_at)
        connection.commit()
        return True, 0
    records = value["records"]
    digest = _sha(records)
    active = connection.execute("SELECT active_generation FROM source_state WHERE source=?",
                                 (source,)).fetchone()[0]
    if active == digest:
        connection.execute("BEGIN IMMEDIATE")
        _source_success(connection, source, digest, value, collected_at)
        connection.commit()
        return True, 0

    staged = connection.execute("SELECT state,cursor,total FROM source_generations WHERE source=? AND generation_id=?",
                                (source, digest)).fetchone()
    if staged and staged["state"] == "staging" and staged["total"] == len(records):
        cursor = staged["cursor"]
    else:
        connection.execute("BEGIN IMMEDIATE")
        stale = connection.execute("SELECT generation_id FROM source_generations WHERE source=? AND state='staging'",
                                   (source,)).fetchall()
        for item in stale:
            connection.execute("DELETE FROM source_generations WHERE source=? AND generation_id=?",
                               (source, item["generation_id"]))
        connection.execute("INSERT OR REPLACE INTO source_generations(source,generation_id,state,cursor,total,created_at) VALUES(?,?,?,?,?,?)",
                           (source, digest, "staging", 0, len(records), collected_at))
        connection.commit()
        cursor = 0

    occurrence = {}
    record_ids = []
    for payload in records:
        encoded = _canonical(payload)
        count = occurrence.get(encoded, 0)
        occurrence[encoded] = count + 1
        record_ids.append(_record_id(source, 0, payload, count))

    while cursor < len(records):
        end = min(cursor + batch_size, len(records))
        connection.execute("BEGIN IMMEDIATE")
        for position in range(cursor, end):
            payload = records[position]
            encoded = _canonical(payload)
            connection.execute("INSERT OR REPLACE INTO source_records(source,generation_id,position,record_id,payload) VALUES(?,?,?,?,?)",
                               (source, digest, position, record_ids[position], encoded))
        cursor = end
        connection.execute("UPDATE source_generations SET cursor=? WHERE source=? AND generation_id=?",
                           (cursor, source, digest))
        connection.commit()
        batches_already += 1
        if batch_limit is not None and batches_already >= batch_limit:
            return False, batches_already

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE source_generations SET state='complete',completed_at=? WHERE source=? AND generation_id=?",
                       (collected_at, source, digest))
    _source_success(connection, source, digest, value, collected_at)
    connection.commit()
    return True, batches_already


def _read_active_records(connection, source):
    state = connection.execute("SELECT active_generation FROM source_state WHERE source=?",
                               (source,)).fetchone()
    generation = state["active_generation"] if state else None
    if not generation:
        return []
    rows = connection.execute("SELECT payload FROM source_records WHERE source=? AND generation_id=? ORDER BY position",
                              (source, generation)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _health_sources(connection):
    rows = connection.execute("SELECT * FROM source_state ORDER BY source").fetchall()
    result = {}
    for row in rows:
        result[row["source"]] = {
            "status": row["status"], "observed_at": row["observed_at"],
            "collected_at": row["collected_at"], "last_success_at": row["last_success_at"],
            "freshness_seconds": row["freshness_seconds"], "error_code": row["error_code"],
            "record_count": row["record_count"], "empty": bool(row["empty"]),
            "quarantined_count": row["quarantined_count"],
            "error_quarantined_count": row["error_quarantined_count"],
        }
    return result


def _review_facts(review_records, preflight_records, review_unknown=0, preflight_unknown=0):
    by_head = {}
    for record in review_records:
        head = record.get("artifact_head")
        if head:
            by_head.setdefault(head, {"artifact_head": head,
                                      "model_acceptance": "unknown",
                                      "owner_acceptance": "unknown",
                                      "provenance_status": "unknown",
                                      "rework_codes": []})
            current = by_head[head]
            current["model_acceptance"] = record["model_acceptance"]
            current["provenance_status"] = record["provenance_status"]
    for record in preflight_records:
        head = record.get("artifact_head")
        if head:
            by_head.setdefault(head, {"artifact_head": head,
                                      "model_acceptance": "unknown",
                                      "owner_acceptance": "unknown",
                                      "provenance_status": "unknown",
                                      "rework_codes": []})
            current = by_head[head]
            current["owner_acceptance"] = record["owner_acceptance"]
            if record["provenance_status"] != "verified":
                current["provenance_status"] = record["provenance_status"]
            current["rework_codes"] = sorted(set(current["rework_codes"])
                                               | set(record["rework_codes"]))
    records = [by_head[key] for key in sorted(by_head)]
    def tally(field):
        counts = {"accepted": 0, "not_accepted": 0, "unknown": 0}
        for record in records:
            value = record[field]
            counts[value if value in counts else "unknown"] += 1
        return counts
    reasons = {}
    for record in records:
        for code in record["rework_codes"]:
            reasons[code] = reasons.get(code, 0) + 1
    return {"schema_version": 1, "unique_artifact_heads": len(records),
            "first_pass": {"model": tally("model_acceptance"),
                           "owner": tally("owner_acceptance")},
            "rework_by_code": dict(sorted(reasons.items())),
            "unknown_head_records": review_unknown + preflight_unknown,
            "records": records,
            "interpretation": "Model acceptance and explicit owner acceptance are separate. Missing provenance or ordering remains unknown."}


def _make_snapshot(connection, *, as_of, generated_at, snapshot_root):
    tracker = _read_active_records(connection, "tracker")
    queue = _read_active_records(connection, "queue")
    event_records = _read_active_records(connection, "events")
    coverage = next((item["value"] for item in event_records if item.get("kind") == "coverage"), {})
    events = [item["value"] for item in event_records if item.get("kind") == "event"]
    event_diagnostics = next((item.get("value", {}) for item in event_records
                              if item.get("kind") == "diagnostics"), {})
    quarantined_count = next((item.get("quarantined_event_count", 0)
                              for item in event_records if item.get("kind") == "diagnostics"), 0)
    event_doc = {"schema": 1, "coverage": coverage, "events": events}
    ingestion_rows = _read_active_records(connection, "ingestion")
    scopes = {"scope-{}".format(index): item.get("value", {})
              for index, item in enumerate(ingestion_rows)}
    ingestion = {"schema": 1, "scopes": scopes}
    snapshot_state = connection.execute("SELECT active_generation FROM source_state WHERE source='snapshots'").fetchone()
    root_for_facts = (snapshot_root if snapshot_state and snapshot_state[0]
                      and os.path.isdir(snapshot_root) else None)
    sources = _health_sources(connection)
    snapshot = report_snapshot.normalize_snapshot(
        tracker, queue, event_doc, ingestion, sources, as_of=as_of,
        generated_at=datetime.fromisoformat(generated_at.replace("Z", "+00:00")),
        snapshot_root=root_for_facts)
    quarantined_ids = {item.get("value", {}).get("event_id") for item in event_records
                       if item.get("kind") == "event" and item.get("quarantined")}
    for event in snapshot["facts"].get("events", []):
        event["quarantined"] = event.get("event_id") in quarantined_ids
    snapshot["facts"]["event_diagnostics"] = event_diagnostics
    snapshot["facts"]["quarantined_event_count"] = quarantined_count
    review_rows = _read_active_records(connection, "reviews")
    preflight_rows = _read_active_records(connection, "preflight")
    review_meta = connection.execute("SELECT quarantined_count FROM source_state WHERE source='reviews'").fetchone()
    preflight_meta = connection.execute("SELECT quarantined_count FROM source_state WHERE source='preflight'").fetchone()
    snapshot["facts"]["review_quality"] = _review_facts(
        review_rows, preflight_rows,
        review_meta[0] if review_meta else 0,
        preflight_meta[0] if preflight_meta else 0)
    snapshot["sources"] = sources
    source_identity = {name: {key: value.get(key) for key in
                       ("observed_at", "freshness_seconds", "error_code", "record_count",
                        "quarantined_count", "error_quarantined_count")}
                       for name, value in sorted(sources.items())}
    content = {"schema_version": SCHEMA_VERSION, "as_of": snapshot["as_of"],
               "sources": source_identity, "facts": snapshot["facts"]}
    snapshot["snapshot_id"] = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    snapshot["generated_at"] = generated_at
    return snapshot


def _publish(connection, snapshot, progress):
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT OR REPLACE INTO snapshots(snapshot_id,generated_at,payload) VALUES(?,?,?)",
                       (snapshot["snapshot_id"], snapshot["generated_at"], _canonical(snapshot)))
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('active_snapshot',?)",
                       (snapshot["snapshot_id"],))
    progress["status"] = "complete"
    progress["published_snapshot_id"] = snapshot["snapshot_id"]
    progress["completed_at"] = snapshot["generated_at"]
    _put_progress(connection, progress)
    old = connection.execute("SELECT snapshot_id FROM snapshots ORDER BY generated_at DESC").fetchall()
    for item in old[SNAPSHOT_HISTORY:]:
        if item["snapshot_id"] != snapshot["snapshot_id"]:
            connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (item["snapshot_id"],))
    for source in SOURCES:
        active = connection.execute("SELECT active_generation FROM source_state WHERE source=?",
                                     (source,)).fetchone()[0]
        connection.execute("DELETE FROM source_generations WHERE source=? AND generation_id<>? AND state<>'staging'",
                           (source, active or ""))
    connection.commit()


def _input_paths(repo=None, tracker=None, queue=None, ledger=None, ingestion=None,
                 snapshot_root=None, review_state=None, preflight_state=None):
    repo = repo or path_setting("JOBSEARCH_REPO", default_repo_dir())
    paths = {
        "tracker": tracker or path_setting("JOBSEARCH_TRACKER", os.path.join(repo, "applications.yaml")),
        "queue": queue or path_setting("JOBSEARCH_QUEUE", default_runtime_path("queue.yaml")),
        "events": ledger or path_setting("JOBSEARCH_RECRUITMENT_LEDGER", default_runtime_path("recruitment_events.json")),
        "ingestion": ingestion or path_setting("JOBSEARCH_INGEST_STATE", default_runtime_path("email_ingest_state.json")),
        "snapshots": snapshot_root or path_setting("JOBSEARCH_SNAPSHOT_ROOT", default_runtime_path("submission-snapshots")),
        "reviews": review_state or path_setting("JOBSEARCH_REVIEW_STATE", default_runtime_path("review_state.json")),
        "preflight": preflight_state or path_setting("JOBSEARCH_PREFLIGHT_STATE", default_runtime_path("preflight_records.json")),
    }
    return paths


def _collect_inputs(paths):
    inputs = {
        "tracker": _read_tracker(paths["tracker"]),
        "queue": _read_queue(paths["queue"]),
        "events": _read_events(paths["events"]),
        "ingestion": _read_ingestion(paths["ingestion"]),
        "snapshots": _read_snapshots(paths["snapshots"]),
        "reviews": _read_reviews(paths["reviews"]),
        "preflight": _read_preflight(paths["preflight"]),
    }
    return {name: _source_payload(name, value) for name, value in inputs.items()}


def collect_projection(*, db_path=None, repo=None, tracker=None, queue=None, ledger=None,
                       ingestion=None, snapshot_root=None, review_state=None,
                       preflight_state=None, as_of=None, now=None,
                       batch_size=DEFAULT_BATCH_SIZE, interrupt_after_batches=None):
    """Collect local sources and atomically publish the newly assembled view.

    ``interrupt_after_batches`` is an in-process test seam. A return with
    ``published=False`` leaves staged rows and their committed cursor resumable.
    """
    if type(batch_size) is not int or batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError("batch size must be between 1 and {}".format(MAX_BATCH_SIZE))
    if as_of is None:
        as_of = date.today().isoformat()
    if not isinstance(as_of, str) or not report_snapshot._day(as_of):
        raise ValueError("as_of must be an ISO calendar date")
    generated_at = _now(now)
    paths = _input_paths(repo, tracker, queue, ledger, ingestion, snapshot_root,
                         review_state, preflight_state)
    inputs = _collect_inputs(paths)
    connection = _connect_writer(db_path or _db_default())
    try:
        progress_row = connection.execute("SELECT value FROM meta WHERE key='collection_progress'").fetchone()
        prior = json.loads(progress_row[0]) if progress_row else None
        progress = prior if isinstance(prior, dict) and prior.get("status") == "in_progress" else {
            "status": "in_progress", "started_at": generated_at, "sources": {}}
        progress.update({"status": "in_progress", "as_of": as_of})
        progress["sources"] = {}
        _put_progress(connection, progress)
        connection.commit()
        batches_done = 0
        for source in SOURCES:
            value = inputs[source]
            done, used = _replace_source(connection, source, value, generated_at, batch_size,
                                         batch_limit=interrupt_after_batches,
                                         batches_already=batches_done)
            batches_done = used
            staging = connection.execute("SELECT cursor,total FROM source_generations WHERE source=? AND state='staging' ORDER BY created_at DESC LIMIT 1",
                                         (source,)).fetchone()
            state = connection.execute("SELECT status,record_count,error_code FROM source_state WHERE source=?",
                                       (source,)).fetchone()
            progress["sources"][source] = {
                "status": "complete" if done else "incomplete",
                "cursor": staging["cursor"] if staging else (len(value["records"]) if value["records"] is not None else 0),
                "total": staging["total"] if staging else (len(value["records"]) if value["records"] is not None else 0),
                "source_status": state["status"], "error_code": state["error_code"],
            }
            _put_progress(connection, progress)
            connection.commit()
            if not done:
                return {"published": False, "progress": progress}
        snapshot = _make_snapshot(connection, as_of=as_of, generated_at=generated_at,
                                  snapshot_root=paths["snapshots"])
        _publish(connection, snapshot, progress)
        return {"published": True, "snapshot_id": snapshot["snapshot_id"],
                "sources": snapshot["sources"]}
    finally:
        connection.close()


def read_snapshot(db_path=None):
    """Return the last fully published sanitized snapshot using read-only SQLite."""
    connection = _connect_reader(db_path or _db_default())
    try:
        active = connection.execute("SELECT value FROM meta WHERE key='active_snapshot'").fetchone()
        if not active:
            raise ProjectionError("cached projection has no published snapshot; run projection.py collect")
        row = connection.execute("SELECT payload FROM snapshots WHERE snapshot_id=?",
                                 (active[0],)).fetchone()
        if not row:
            raise ProjectionError("cached projection snapshot is unavailable")
        snapshot = json.loads(row["payload"])
        read_at = datetime.now(timezone.utc)
        for source in snapshot.get("sources", {}).values():
            observed = _timestamp(source.get("observed_at"))
            collected = _timestamp(source.get("collected_at"))
            source["age_seconds"] = (
                max(0, int((read_at - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds()))
                if observed else None)
            source["collection_age_seconds"] = (
                max(0, int((read_at - datetime.fromisoformat(collected.replace("Z", "+00:00"))).total_seconds()))
                if collected else None)
            if (source.get("status") == "fresh" and source["age_seconds"] is not None
                    and source["age_seconds"] > source.get("freshness_seconds", 0)):
                source["status"] = "stale"
        progress_row = connection.execute("SELECT value FROM meta WHERE key='collection_progress'").fetchone()
        progress = json.loads(progress_row[0]) if progress_row else {"status": "idle"}
        if progress.get("status") == "in_progress":
            snapshot["collection"] = {
                "status": "in_progress",
                "started_at": progress.get("started_at"),
                "sources": progress.get("sources", {}),
            }
        else:
            snapshot["collection"] = {"status": "complete"}
        return snapshot
    finally:
        connection.close()


def format_compact(snapshot):
    """Shared compact status line block for CLI, a future web view, or command."""
    facts = snapshot.get("facts", {})
    attempts = facts.get("attempts", [])
    statuses = {}
    for item in attempts:
        state = item.get("status") if isinstance(item, dict) else None
        if isinstance(state, str) and CODE_RE.fullmatch(state.lower()):
            statuses[state] = statuses.get(state, 0) + 1
    sources = snapshot.get("sources", {})
    def source_status(value):
        status = value.get("status", "unavailable")
        if value.get("error_code"):
            status += "/" + value["error_code"]
        age = value.get("age_seconds")
        status += " age=" + ("unknown" if age is None else str(age) + "s")
        if value.get("collection_age_seconds") is not None:
            status += " collected=" + str(value["collection_age_seconds"]) + "s"
        return status
    health = ", ".join("{}={}".format(name, source_status(value))
                       for name, value in sorted(sources.items()))
    review = facts.get("review_quality", {})
    model = review.get("first_pass", {}).get("model", {})
    owner = review.get("first_pass", {}).get("owner", {})
    lines = ["Jobs snapshot {} · as of {} · generated {}".format(
        snapshot.get("snapshot_id", "unknown"), snapshot.get("as_of", "unknown"),
        snapshot.get("generated_at", "unknown")),
        "Queue: " + (", ".join("{} {}".format(key, value)
                                for key, value in sorted(statuses.items())) or "empty"),
        "Review heads: {} · model {}/{}/{} accepted/not/unknown · owner {}/{}/{}".format(
            review.get("unique_artifact_heads", 0), model.get("accepted", 0),
            model.get("not_accepted", 0), model.get("unknown", 0),
            owner.get("accepted", 0), owner.get("not_accepted", 0), owner.get("unknown", 0)),
        "Sources: " + health]
    collection = snapshot.get("collection", {})
    if collection.get("status") == "in_progress":
        unfinished = [name for name, state in collection.get("sources", {}).items()
                      if state.get("status") == "incomplete"]
        lines.append("Collection: in progress" + (" (" + ", ".join(unfinished) + ")" if unfinished else ""))
    return "\n".join(lines)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="read local sources and publish a cached snapshot")
    collect.add_argument("--db", default=None)
    collect.add_argument("--repo", default=None)
    collect.add_argument("--tracker", default=None)
    collect.add_argument("--queue", default=None)
    collect.add_argument("--ledger", default=None)
    collect.add_argument("--ingest-state", default=None)
    collect.add_argument("--snapshot-root", default=None)
    collect.add_argument("--review-state", default=None)
    collect.add_argument("--preflight-state", default=None)
    collect.add_argument("--as-of", default=None)
    collect.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    read = commands.add_parser("read", help="read the cached versioned snapshot (read-only)")
    read.add_argument("--db", default=None)
    read.add_argument("--json", action="store_true")
    report = commands.add_parser("report", help="render a cached report (read-only)")
    report.add_argument("kind", choices=("compact", "health", "funnel"))
    report.add_argument("--db", default=None)
    report.add_argument("--as-of", default=None)
    report.add_argument("--timezone", default=os.environ.get("JOBSEARCH_TIMEZONE", "Asia/Singapore"))
    report.add_argument("--weekday-goal", type=int, default=1)
    report.add_argument("--weekend-goal", type=int, default=0)
    report.add_argument("--ready-buffer", type=int, default=1)
    report.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect":
            as_of = args.as_of or datetime.now(report_snapshot.timezone_for(
                os.environ.get("JOBSEARCH_TIMEZONE", "Asia/Singapore"))).date().isoformat()
            result = collect_projection(
                db_path=args.db, repo=args.repo, tracker=args.tracker, queue=args.queue,
                ledger=args.ledger, ingestion=args.ingest_state,
                snapshot_root=args.snapshot_root, review_state=args.review_state,
                preflight_state=args.preflight_state, as_of=as_of,
                batch_size=args.batch_size)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["published"] else 1
        snapshot = read_snapshot(args.db)
        if args.command == "read":
            print(json.dumps(snapshot, indent=2, ensure_ascii=False) if args.json
                  else format_compact(snapshot))
            return 0
        if args.kind == "compact":
            if args.json:
                print(json.dumps({"snapshot_id": snapshot["snapshot_id"],
                                  "as_of": snapshot["as_of"],
                                  "generated_at": snapshot["generated_at"],
                                  "text": format_compact(snapshot)}, indent=2))
            else:
                print(format_compact(snapshot))
            return 0
        if args.kind == "health":
            import pipeline_health
            if min(args.weekday_goal, args.weekend_goal, args.ready_buffer) < 0:
                raise ValueError("goals and ready buffer must be non-negative")
            report = pipeline_health.build_report(snapshot, timezone_name=args.timezone,
                                                  weekday_goal=args.weekday_goal,
                                                  weekend_goal=args.weekend_goal,
                                                  ready_buffer=args.ready_buffer,
                                                  as_of=args.as_of)
            renderer = pipeline_health.render_text
        else:
            import funnel_report
            report = funnel_report.build_report(snapshot, as_of=args.as_of,
                                                timezone_name=args.timezone)
            renderer = funnel_report.render_text
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else renderer(report))
        return 0
    except (ProjectionError, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, ProjectionError) else type(exc).__name__
        print("projection: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
