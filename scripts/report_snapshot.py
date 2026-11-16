#!/usr/bin/env python3
"""Allowlisted, in-memory snapshot seam for deterministic local reports.

Only ``collect_snapshot`` reads source files. Report builders consume this
normalized shape and never perform I/O. The schema is intentionally small so
the later SQLite projection can persist the same facts without retaining
correspondence, notes, answers, or resume bytes.
"""
import hashlib
import importlib.util
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import application_identity


SCHEMA_VERSION = 1
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_FRESHNESS_SECONDS = {
    "tracker": 14 * 86400,
    "queue": 86400,
    "events": 7 * 86400,
    "ingestion": 86400,
    "snapshots": 86400,
}
_APP_FIELDS = ("company", "role", "link", "pr", "pr_number", "source",
               "status", "stage", "stage_since", "applied_date", "submitted_at")
_QUEUE_FIELDS = ("company", "role", "link", "pr", "pr_number", "source",
                "status", "submitted_at", "claimed_at", "attempt_id", "tracker_sync",
                "snapshot_id", "prepared_snapshot_id", "snapshot_provenance",
                "attempt_state", "blocker_code")


def _iso_now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("collection time must include a timezone")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timezone_for(name):
    """Resolve IANA zones; retain the project's fixed UTC+8 fallback on Windows."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Singapore":
            return timezone(timedelta(hours=8), "Asia/Singapore")
        raise


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
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value.strip() else None


def _safe_text(value, limit=160):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > limit or any(ord(ch) < 32 for ch in value):
        return None
    return value


def _sha(value):
    return value.lower() if isinstance(value, str) and HASH_RE.fullmatch(value.lower()) else None


def _allow_fields(row, fields):
    result = {}
    for key in fields:
        value = row.get(key)
        if key in ("company", "role", "source", "status", "stage", "tracker_sync",
                   "snapshot_provenance", "attempt_state", "blocker_code", "attempt_id"):
            value = _safe_text(value, 120 if key in ("role", "company") else 80)
        elif key in ("applied_date", "stage_since"):
            value = _day(value)
        elif key in ("submitted_at", "claimed_at"):
            value = _timestamp(value) or _day(value)
        elif key in ("resume_hash", "resume_sha256", "resume_pdf_sha256",
                     "resume_version_sha256", "resume_version_hash",
                     "submitted_resume_sha256"):
            value = _sha(value)
        elif key in ("link",):
            value = _safe_text(value, 2048)
        elif key in ("pr", "pr_number"):
            value = value if isinstance(value, (str, int)) else None
        elif key in ("snapshot_id", "prepared_snapshot_id"):
            value = value if isinstance(value, str) and HASH_RE.fullmatch(value) else None
        if value is not None:
            result[key] = value
    return result


def _source(name, status, now, *, observed_at=None, error_code=None, count=None):
    observed = _timestamp(observed_at)
    collected = _iso_now(now)
    state = status
    if status == "fresh" and observed is None:
        state = "stale"
        error_code = error_code or "observation_time_unknown"
    if status == "fresh" and observed:
        try:
            age = max(0, (datetime.fromisoformat(collected.replace("Z", "+00:00"))
                          - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds())
            if age > SOURCE_FRESHNESS_SECONDS[name]:
                state = "stale"
        except ValueError:
            observed = None
    return {"status": state, "observed_at": observed,
            "collected_at": collected,
            "last_success_at": collected if status == "fresh" else None,
            "freshness_seconds": SOURCE_FRESHNESS_SECONDS[name],
            "error_code": error_code, "record_count": count}


def _read_yaml(path, parser, name, now):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        rows = parser(raw)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid rows")
        if name == "tracker" and not rows and not any(
                line.strip() == "[]" for line in raw.splitlines()):
            raise ValueError("tracker is not an explicit empty list")
        # File modification time says when bytes changed, not when the facts
        # were observed. These sources have no observation-time contract.
        return rows, _source(name, "fresh", now, observed_at=None, count=len(rows))
    except FileNotFoundError:
        return [], _source(name, "unavailable", now, error_code="source_missing")
    except (OSError, UnicodeError, ValueError, TypeError):
        return [], _source(name, "unavailable", now, error_code="source_invalid")


def _read_json(path, name, now):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh, object_pairs_hook=_unique_json_object)
        # Reading/touching a ledger does not establish when its facts were observed.
        return value, _source(name, "fresh", now)
    except FileNotFoundError:
        return None, _source(name, "unavailable", now, error_code="source_missing")
    except (OSError, UnicodeError, ValueError, TypeError):
        return None, _source(name, "unavailable", now, error_code="source_invalid")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_resume_hash(snapshot_root, manifest_id, application_id, attempt_id):
    """Read only allowlisted PDF hash metadata; never read retained artifact bytes."""
    if not isinstance(manifest_id, str) or not HASH_RE.fullmatch(manifest_id):
        return None
    path = os.path.join(snapshot_root, "manifests", manifest_id + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh, object_pairs_hook=_unique_json_object)
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                or manifest.get("snapshot_id", manifest_id) != manifest_id):
            return None
        canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != manifest_id:
            return None
        confirmation = manifest.get("confirmation")
        if not isinstance(confirmation, dict) or not confirmation.get("submitted_at"):
            return None
        if (manifest.get("canonical_application_id") != application_id
                or manifest.get("attempt_id") != attempt_id):
            return None
        artifacts = manifest.get("artifacts")
        resume = artifacts.get("resume_pdf") if isinstance(artifacts, dict) else None
        return _sha(resume.get("sha256")) if isinstance(resume, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def normalize_snapshot(tracker_rows, queue_rows, event_doc, ingestion_state,
                       sources, *, as_of, generated_at, snapshot_root=None,
                       snapshot_facts=None):
    """Build the versioned allowlisted facts payload from already-read sources."""
    if not _day(as_of):
        raise ValueError("as_of must be an ISO calendar date")
    resolved = application_identity.resolve_applications(tracker_rows, queue_rows)
    import summary as summary_module
    applications = []
    for canonical_id, record in sorted(resolved["applications"].items()):
        tr = [_allow_fields(row, _APP_FIELDS) for row in record["tracker_rows"]]
        qr = [_allow_fields(row, _QUEUE_FIELDS) for row in record["queue_rows"]]
        row = _allow_fields(record["row"], _APP_FIELDS)
        date_claims = []
        submission_moments = []
        for raw in record["tracker_rows"]:
            value = raw.get("applied_date")
            if value or summary_module.row_stage(raw) in summary_module.SUBMITTED_STATUSES:
                date_claims.append({"source": "tracker", "value": _day(value),
                                    "invalid": bool(value and not _day(value)),
                                    "missing": not bool(value)})
        for raw in record["queue_rows"]:
            if str(raw.get("status") or "").lower() != "submitted":
                continue
            value = raw.get("submitted_at")
            stamp = _timestamp(value)
            claim = stamp or _day(value)
            date_claims.append({"source": "queue", "value": claim,
                                "invalid": bool(value and not claim),
                                "missing": not bool(value)})
            if stamp:
                submission_moments.append(stamp)
        resume_hash = None
        snapshots_available = (sources.get("snapshots", {}).get("status") in ("fresh", "stale"))
        for attempt in qr:
            attempt["snapshot_manifest_status"] = "not_claimed"
            if (attempt.get("status") != "submitted"
                    or attempt.get("snapshot_provenance") != "complete"
                    or not attempt.get("snapshot_id")):
                continue
            if (snapshot_facts is None and not snapshot_root) or not snapshots_available:
                attempt["snapshot_manifest_status"] = "unavailable"
                continue
            if snapshot_facts is not None:
                manifest = snapshot_facts.get(attempt.get("snapshot_id"), {})
                candidate = (manifest.get("resume_hash")
                             if manifest.get("application_id") == canonical_id
                             and manifest.get("attempt_id") == attempt.get("attempt_id") else None)
            else:
                candidate = _read_resume_hash(snapshot_root, attempt.get("snapshot_id"),
                                              canonical_id, attempt.get("attempt_id"))
            attempt["snapshot_manifest_status"] = "verified" if candidate else "missing_or_invalid"
            if candidate and not resume_hash:
                resume_hash = candidate
        applications.append({
            "application_id": canonical_id,
            "aliases": record["aliases"],
            "row": row,
            "tracker_rows": tr,
            "queue_rows": qr,
            "submission_date_claims": date_claims,
            "submission_moments": sorted(set(submission_moments)),
            "resume_hash": resume_hash,
            "resolution_method": record["resolution"]["method"],
        })

    conflicted_aliases = {alias for conflict in resolved["conflicts"]
                          for alias in conflict.get("aliases", [])}
    attempts = []
    for raw in queue_rows:
        attempt = _allow_fields(raw, _QUEUE_FIELDS)
        try:
            row_identity = application_identity.application_id(raw)
        except ValueError:
            row_identity = None
        canonical = (resolved["alias_to_canonical"].get(row_identity)
                     if row_identity and row_identity not in conflicted_aliases else row_identity)
        attempt["application_id"] = canonical
        attempt["snapshot_manifest_status"] = "not_claimed"
        if (attempt.get("status") == "submitted"
                and attempt.get("snapshot_provenance") == "complete"
                and attempt.get("snapshot_id")):
            joined = next((app for app in applications
                           if app["application_id"] == canonical), None)
            joined_attempt = next((item for item in joined["queue_rows"]
                                   if attempt.get("attempt_id")
                                   and item.get("attempt_id") == attempt.get("attempt_id")), None) if joined else None
            attempt["snapshot_manifest_status"] = (
                joined_attempt.get("snapshot_manifest_status") if joined_attempt else "unavailable")
        attempts.append(attempt)

    safe_sources = {key: dict(value) for key, value in sorted(sources.items())}
    events, quarantined, evidence_diagnostics = [], [], {}
    coverage = {}
    if isinstance(event_doc, dict):
        raw_events = event_doc.get("events")
        coverage_raw = event_doc.get("coverage")
        if isinstance(raw_events, list) and isinstance(coverage_raw, dict):
            try:
                import recruitment_evidence
                effective, quarantined_ids, evidence_diagnostics = recruitment_evidence.effective_events(event_doc)
                supported_types = recruitment_evidence.EVENT_TYPES
                allowed = ("event_id", "revision", "revision_id", "supersedes", "event_type",
                           "application_id", "occurred_at", "verification", "origin")
                for event in effective:
                    normalized = {key: event.get(key) for key in allowed if key in event}
                    if normalized.get("event_type") not in supported_types:
                        normalized["event_type"] = None
                    normalized["quarantined"] = event.get("event_id") in quarantined_ids
                    events.append(normalized)
                quarantined = sorted(quarantined_ids)
                for key in ("complete", "transport_complete"):
                    if type(coverage_raw.get(key)) is bool:
                        coverage[key] = coverage_raw[key]
                for key in ("last_scan_at", "observed_at"):
                    value = _timestamp(coverage_raw.get(key))
                    if value:
                        coverage[key] = value
                for key in ("since", "until"):
                    value = _day(coverage_raw.get(key))
                    if value:
                        coverage[key] = value
                if safe_sources.get("events", {}).get("status") != "unavailable":
                    safe_sources["events"] = _source(
                        "events", "fresh", generated_at,
                        observed_at=coverage.get("observed_at") or coverage.get("last_scan_at"),
                        count=len(raw_events))
            except (TypeError, ValueError, KeyError):
                evidence_diagnostics = {"invalid_ledger": 1}
                health = safe_sources.get("events", {})
                health.update({"status": "unavailable", "error_code": "source_invalid"})
                safe_sources["events"] = health
    ingestion = {}
    if isinstance(ingestion_state, dict) and ingestion_state.get("schema") == 1:
        scopes = ingestion_state.get("scopes")
        if isinstance(scopes, dict):
            stamps = [_timestamp(value.get("last_scan_at")) for value in scopes.values()
                      if isinstance(value, dict)]
            coverage_scan = _timestamp(coverage.get("last_scan_at"))
            if coverage_scan:
                stamps.append(coverage_scan)
            ingestion = {"scope_count": len(scopes),
                         "last_scan_at": max((stamp for stamp in stamps if stamp), default=None)}
            if safe_sources.get("ingestion", {}).get("status") != "unavailable":
                safe_sources["ingestion"] = _source(
                    "ingestion", "fresh", generated_at,
                    observed_at=ingestion["last_scan_at"], count=len(scopes))

    facts = {
        "applications": applications,
        "attempts": attempts,
        "identity_conflicts": [{"reason": item["reason"],
                                "row_refs": item.get("row_refs", [])}
                               for item in resolved["conflicts"]],
        "application_aliases": dict(sorted(resolved["alias_to_canonical"].items())),
        "conflicted_application_aliases": sorted({
            alias for conflict in resolved["conflicts"]
            for alias in conflict.get("aliases", [])}),
        "identity_diagnostics": dict(sorted(resolved["diagnostics"].items())),
        "events": events,
        "quarantined_event_count": len(quarantined),
        "event_diagnostics": dict(sorted(evidence_diagnostics.items())),
        "event_coverage": coverage,
        "ingestion": ingestion,
    }
    source_identity = {
        name: {key: value.get(key) for key in
               ("observed_at", "freshness_seconds", "error_code", "record_count")}
        for name, value in safe_sources.items()
    }
    content = {"schema_version": SCHEMA_VERSION, "as_of": _day(as_of),
               "sources": source_identity, "facts": facts}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema_version": SCHEMA_VERSION, "snapshot_id": digest,
            "generated_at": _iso_now(generated_at), "as_of": _day(as_of),
            "sources": safe_sources, "facts": facts}


def collect_snapshot(*, tracker_path, queue_path, ledger_path, ingestion_path,
                     snapshot_root, as_of, now=None):
    """Named I/O boundary: read private local sources and normalize once."""
    import summary as summary_module

    queue_pathname = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.py")
    queue_spec = importlib.util.spec_from_file_location("_jobsearch_report_queue", queue_pathname)
    if queue_spec is None or queue_spec.loader is None:
        raise RuntimeError("queue parser is unavailable")
    queue_module = importlib.util.module_from_spec(queue_spec)
    queue_spec.loader.exec_module(queue_module)

    now = now or datetime.now(timezone.utc)
    tracker_rows, tracker_health = _read_yaml(tracker_path, summary_module.parse_yaml_list,
                                               "tracker", now)
    queue_rows, queue_health = _read_yaml(queue_path,
                                           lambda value: queue_module.parse_queue(value),
                                           "queue", now)
    event_doc, event_health = _read_json(ledger_path, "events", now)
    if event_health["status"] != "unavailable" and (
            not isinstance(event_doc, dict) or event_doc.get("schema") != 1
            or not isinstance(event_doc.get("coverage"), dict)
            or not isinstance(event_doc.get("events"), list)):
        event_doc = None
        event_health = _source("events", "unavailable", now, error_code="source_invalid")
    ingestion_state, ingestion_health = _read_json(ingestion_path, "ingestion", now)
    if ingestion_health["status"] != "unavailable" and (
            not isinstance(ingestion_state, dict) or ingestion_state.get("schema") != 1
            or not isinstance(ingestion_state.get("scopes"), dict)):
        ingestion_state = None
        ingestion_health = _source("ingestion", "unavailable", now, error_code="source_invalid")
    if os.path.isdir(snapshot_root):
        snapshot_health = _source("snapshots", "fresh", now)
    else:
        snapshot_health = _source("snapshots", "unavailable", now, error_code="source_missing")
    sources = {"tracker": tracker_health, "queue": queue_health,
               "events": event_health, "ingestion": ingestion_health,
               "snapshots": snapshot_health}
    return normalize_snapshot(tracker_rows, queue_rows, event_doc, ingestion_state,
                              sources, as_of=as_of, generated_at=now,
                              snapshot_root=snapshot_root)
