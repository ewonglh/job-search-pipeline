#!/usr/bin/env python3
"""Bounded, allowlisted import of OpenClaw cron runs and trajectory exports.

This adapter is pinned to OpenClaw 2026.10.1 (e178c39). Collection is explicit;
cached readers never run it. Trajectory bundles are parsed in place and only
the small allowlisted projection is returned.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone


NATIVE_VERSION = "2026.10.1"
NATIVE_COMMIT = "e178c39"
INTERRUPTED_ERROR = "cron: job interrupted by gateway restart"
TRAJECTORY_SCHEMA = "openclaw-trajectory"
TRAJECTORY_VERSION = 1
PAGE_SIZE_MAX = 200
PAGE_COUNT_MAX = 1000
RUN_COUNT_MAX = PAGE_SIZE_MAX * PAGE_COUNT_MAX
EXPORT_COUNT_MAX = 100
EVENT_COUNT_MAX = 20000
EVENT_TOTAL_COUNT_MAX = 100000
MANIFEST_BYTES_MAX = 256 * 1024
EVENT_FILE_BYTES_MAX = 25 * 1024 * 1024
EXPORT_TOTAL_BYTES_MAX = 64 * 1024 * 1024
EVENT_LINE_BYTES_MAX = 256 * 1024
CLI_OUTPUT_BYTES_MAX = 8 * 1024 * 1024
CLI_COLLECTION_BYTES_MAX = 64 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 15
COLLECTION_TIMEOUT_SECONDS = 180
RETENTION_DEFAULT = 500
RETENTION_MAX = 10000

RUN_FIELDS = frozenset((
    "ts", "jobId", "action", "status", "completionStatus", "error",
    "errorReason", "summary", "diagnostics", "delivered", "deliveryStatus",
    "deliveryError", "deliverySuppressionReason", "failureNotificationDelivery",
    "delivery", "sessionId", "sessionKey", "runId", "runAtMs", "durationMs",
    "nextRunAtMs", "triggerFired", "model", "provider", "usage", "jobName",
))
PAGE_FIELDS = frozenset((
    "entries", "total", "offset", "limit", "hasMore", "nextOffset",
))
EVENT_FIELDS = frozenset((
    "traceSchema", "schemaVersion", "traceId", "source", "type", "ts",
    "seq", "sourceSeq", "sessionId", "sessionKey", "workspaceDir", "entryId",
    "parentEntryId", "runId", "data",
))
STATUS_VALUES = frozenset(("ok", "error", "skipped"))
COMPLETION_VALUES = frozenset(("succeeded", "failed", "unknown"))
DELIVERY_VALUES = frozenset(("delivered", "not-delivered", "unknown", "not-requested"))
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,255}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,127}$")
VERSION_RE = re.compile(r"\b(\d{4}\.\d+\.\d+)\b")
COMMIT_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.IGNORECASE)


class TraceError(Exception):
    """An error code safe to expose without source text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json_loads(value):
    return json.loads(value, object_pairs_hook=_unique_object,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid number")))


def _bounded_id(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise TraceError("unsupported_schema")
    return value


def _bounded_name(value):
    if value is None:
        return None
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        return None
    return value


def _integer(value, *, minimum=0, maximum=10 ** 15, optional=True):
    if value is None and optional:
        return None
    if type(value) is not int or value < minimum or value > maximum:
        raise TraceError("unsupported_schema")
    return value


def _safe_counter(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraceError("unsupported_schema")
    if not math.isfinite(value) or value < 0 or value > 10 ** 15:
        raise TraceError("unsupported_schema")
    return value


def _iso_ms(value):
    stamp = _integer(value)
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        raise TraceError("unsupported_schema")


def _iso_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_ms(value):
    parsed = _iso_timestamp(value)
    if parsed is None:
        return None
    stamp = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    return int(stamp.timestamp() * 1000)


def _normalize_run(entry):
    if not isinstance(entry, dict) or not set(entry).issubset(RUN_FIELDS):
        raise TraceError("unsupported_schema")
    _integer(entry.get("ts"), optional=False)
    if entry.get("action") != "finished":
        raise TraceError("unsupported_schema")
    status = entry.get("status")
    completion = entry.get("completionStatus")
    delivery = entry.get("deliveryStatus")
    if status is not None and (not isinstance(status, str) or status not in STATUS_VALUES):
        raise TraceError("unsupported_schema")
    if completion is not None and (not isinstance(completion, str) or completion not in COMPLETION_VALUES):
        raise TraceError("unsupported_schema")
    if delivery is not None and (not isinstance(delivery, str) or delivery not in DELIVERY_VALUES):
        raise TraceError("unsupported_schema")
    delivered = entry.get("delivered")
    trigger_fired = entry.get("triggerFired")
    if delivered is not None and type(delivered) is not bool:
        raise TraceError("unsupported_schema")
    if trigger_fired is not None and type(trigger_fired) is not bool:
        raise TraceError("unsupported_schema")
    job_id = _bounded_id(entry.get("jobId"), required=True)
    run_id = _bounded_id(entry.get("runId"))
    session_id = _bounded_id(entry.get("sessionId"))
    session_key = _bounded_id(entry.get("sessionKey"))
    run_at_ms = _integer(entry.get("runAtMs"))
    duration_ms = _integer(entry.get("durationMs"), maximum=10 ** 12)
    finished_at = _iso_ms(entry.get("ts"))
    started_at = _iso_ms(run_at_ms)

    provider = _bounded_name(entry.get("provider"))
    model = _bounded_name(entry.get("model"))
    raw_usage = entry.get("usage")
    usage = None
    if raw_usage is not None:
        if not isinstance(raw_usage, dict) or not set(raw_usage).issubset({
                "input_tokens", "output_tokens", "total_tokens",
                "cache_read_tokens", "cache_write_tokens"}):
            raise TraceError("unsupported_schema")
        usage = {
            "scope": "cron_run",
            "provider": provider,
            "input_tokens": _safe_counter(raw_usage.get("input_tokens")),
            "output_tokens": _safe_counter(raw_usage.get("output_tokens")),
            "cache_read_tokens": _safe_counter(raw_usage.get("cache_read_tokens")),
            "cache_write_tokens": _safe_counter(raw_usage.get("cache_write_tokens")),
            "cost_status": "unavailable",
        }

    if completion is None:
        if status in ("error", "skipped"):
            completion = "failed"
        elif status == "ok" and (delivered is True or delivery in ("delivered", "not-requested")):
            completion = "succeeded"
        else:
            completion = "unknown"
    interrupted = status == "error" and entry.get("error") == INTERRUPTED_ERROR
    if interrupted:
        execution = "interrupted"
    elif status == "ok":
        execution = "succeeded"
    elif status == "error":
        execution = "failed"
    elif status == "skipped":
        execution = "skipped"
    else:
        execution = "unknown"
    if trigger_fired is False:
        business = "no_work"
    else:
        business = "unknown"
    if delivery == "delivered" or delivered is True:
        delivery_result = "delivered"
    elif delivery == "not-requested":
        delivery_result = "not_requested"
    elif delivery == "not-delivered" and isinstance(entry.get("deliverySuppressionReason"), str):
        delivery_result = "suppressed"
    elif delivery == "not-delivered":
        delivery_result = "not_delivered"
    else:
        delivery_result = "unknown"

    attention = []
    if status is None:
        attention.append("missing_completion")
    elif interrupted:
        attention.append("scheduler_interrupted")
    elif status == "error":
        attention.append("scheduler_failed")
    if completion == "failed" and status != "skipped":
        attention.append("completion_failed")
    if status == "skipped" or trigger_fired is False:
        attention.append("planned_skip")
    if completion == "unknown":
        attention.append("completion_unknown")
    if business == "unknown":
        attention.append("business_result_unknown")
    if delivery_result == "unknown":
        attention.append("delivery_unknown")
    if run_id is None:
        attention.append("run_id_unknown")
    if session_id is None:
        attention.append("session_join_unknown")

    return {
        "kind": "run",
        "run_id": run_id,
        "job_id": job_id,
        "session_id": session_id,
        "session_key": session_key,
        "scheduler_status": status,
        "execution_result": execution,
        "completion_status": completion,
        "business_result": business,
        "application_join_status": "unknown",
        "artifact_join_status": "unknown",
        "delivery_status": delivery_result,
        "trigger_fired": trigger_fired,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "provider": provider,
        "model": model,
        "usage": usage,
        "tool_trace_status": "missing",
        "tool_events": [],
        "retry_context": "unknown",
        "attention_codes": attention,
        "_finished_ms": _integer(entry.get("ts"), optional=False),
    }


def _parse_page(stdout, *, expected_offset, requested_limit):
    if isinstance(stdout, bytes):
        if len(stdout) > CLI_OUTPUT_BYTES_MAX:
            raise TraceError("native_output_too_large")
        try:
            stdout = stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise TraceError("unsupported_schema")
    if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > CLI_OUTPUT_BYTES_MAX:
        raise TraceError("native_output_too_large")
    try:
        page = _json_loads(stdout)
    except (ValueError, TypeError):
        raise TraceError("unsupported_schema")
    if not isinstance(page, dict) or not set(page).issubset(PAGE_FIELDS):
        raise TraceError("unsupported_schema")
    entries = page.get("entries")
    total = _integer(page.get("total"), optional=False, maximum=RUN_COUNT_MAX)
    offset = _integer(page.get("offset"), optional=False, maximum=RUN_COUNT_MAX)
    limit = _integer(page.get("limit"), optional=False, maximum=PAGE_SIZE_MAX)
    has_more = page.get("hasMore")
    next_offset = page.get("nextOffset")
    if (not isinstance(entries, list) or len(entries) > requested_limit
            or offset != expected_offset or limit != requested_limit
            or type(has_more) is not bool):
        raise TraceError("unsupported_schema")
    if next_offset is not None:
        next_offset = _integer(next_offset, optional=False, maximum=RUN_COUNT_MAX)
    if has_more:
        if next_offset != offset + len(entries) or next_offset <= offset or next_offset >= total:
            raise TraceError("page_incomplete")
    elif next_offset is not None or offset + len(entries) != total:
        raise TraceError("page_incomplete")
    normalized = [_normalize_run(entry) for entry in entries]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": next_offset,
        "entries": normalized,
        "digest": hashlib.sha256(json.dumps(normalized, sort_keys=True,
                                              separators=(",", ":")).encode("utf-8")).hexdigest(),
    }


def _invoke(runner, argv, *, timeout=COMMAND_TIMEOUT_SECONDS):
    try:
        result = runner(argv, capture_output=True, text=True,
                        timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise TraceError("native_timeout")
    except (OSError, ValueError):
        raise TraceError("native_unavailable")
    if result.returncode != 0:
        raise TraceError("native_command_failed")
    return result.stdout


def _version_supported(stdout):
    if isinstance(stdout, bytes):
        try:
            stdout = stdout.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(stdout, str):
        return False
    version = VERSION_RE.search(stdout)
    commit = COMMIT_RE.search(stdout)
    return bool(version and version.group(1) == NATIVE_VERSION
                and commit and commit.group(1).lower().startswith(NATIVE_COMMIT))


def _read_page_pass(runner, *, binary, page_size, max_pages, deadline,
                    byte_budget=CLI_COLLECTION_BYTES_MAX):
    pages = []
    entries = []
    offset = 0
    total = None
    bytes_read = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TraceError("collection_timeout")
        if len(pages) >= max_pages:
            raise TraceError("page_limit")
        stdout = _invoke(runner, [
            binary, "cron", "runs", "--all", "--json", "--limit",
            str(page_size), "--offset", str(offset),
        ], timeout=min(COMMAND_TIMEOUT_SECONDS, remaining))
        if time.monotonic() > deadline:
            raise TraceError("collection_timeout")
        if isinstance(stdout, bytes):
            page_bytes = len(stdout)
        elif isinstance(stdout, str):
            page_bytes = len(stdout.encode("utf-8"))
        else:
            raise TraceError("unsupported_schema")
        bytes_read += page_bytes
        if bytes_read > byte_budget:
            raise TraceError("native_output_too_large")
        page = _parse_page(stdout, expected_offset=offset, requested_limit=page_size)
        if total is None:
            total = page["total"]
        if page["total"] != total:
            raise TraceError("snapshot_changed")
        pages.append((page["total"], page["offset"], page["digest"]))
        entries.extend(page["entries"])
        if len(entries) > RUN_COUNT_MAX:
            raise TraceError("record_limit")
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    return entries, pages, total, bytes_read


def _dedupe_runs(entries):
    by_id = {}
    without_id = []
    for entry in entries:
        run_id = entry["run_id"]
        if run_id is None:
            without_id.append(entry)
            continue
        previous = by_id.get(run_id)
        if previous is None:
            by_id[run_id] = entry
            continue
        if _run_public(previous) != _run_public(entry):
            raise TraceError("duplicate_run_id_conflict")
    return list(by_id.values()) + without_id


def _run_public(record):
    return {key: value for key, value in record.items() if not key.startswith("_")}


def _event_timestamp(event):
    stamp = _iso_timestamp(event.get("ts"))
    if stamp is None:
        raise TraceError("trajectory_export_invalid")
    return stamp


def _event_id(event):
    source = event.get("source")
    if source == "runtime":
        sequence = event.get("seq")
        if type(sequence) is int and sequence >= 0:
            return ("runtime", sequence)
    if source == "transcript":
        entry_id = event.get("entryId")
        source_seq = event.get("sourceSeq")
        if type(source_seq) is int and source_seq > 0:
            if entry_id is None:
                return ("transcript-sequence", source_seq)
            if isinstance(entry_id, str) and ID_RE.fullmatch(entry_id):
                return ("transcript", entry_id, source_seq)
    return None


def _parse_export_directory(directory, *, deadline=None):
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    events_path = directory / "events.jsonl"
    try:
        if directory.is_symlink() or manifest_path.is_symlink() or events_path.is_symlink():
            raise TraceError("trajectory_export_invalid")
        if manifest_path.stat().st_size > MANIFEST_BYTES_MAX or events_path.stat().st_size > EVENT_FILE_BYTES_MAX:
            raise TraceError("trajectory_export_invalid")
        manifest = _json_loads(manifest_path.read_text(encoding="utf-8"))
    except TraceError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise TraceError("trajectory_export_invalid")
    if (not isinstance(manifest, dict) or manifest.get("traceSchema") != TRAJECTORY_SCHEMA
            or type(manifest.get("schemaVersion")) is not int
            or manifest.get("schemaVersion") != TRAJECTORY_VERSION):
        raise TraceError("unsupported_trajectory_version")
    session_id = _bounded_id(manifest.get("sessionId"), required=True)
    trace_id = _bounded_id(manifest.get("traceId"), required=True)
    if trace_id != session_id or _iso_timestamp(manifest.get("generatedAt")) is None:
        raise TraceError("trajectory_export_invalid")
    declared_count = _integer(manifest.get("eventCount"), optional=False, maximum=EVENT_COUNT_MAX)
    parsed_line_count = 0
    seen_native_ids = {}
    seen_tool_events = {}
    try:
        with events_path.open("rb") as source:
            for line_number, raw_line in enumerate(source, 1):
                if deadline is not None and time.monotonic() > deadline:
                    raise TraceError("collection_timeout")
                if line_number > EVENT_COUNT_MAX or len(raw_line) > EVENT_LINE_BYTES_MAX:
                    raise TraceError("trajectory_event_limit")
                if not raw_line.strip():
                    continue
                parsed_line_count += 1
                try:
                    event = _json_loads(raw_line.decode("utf-8"))
                except (UnicodeError, ValueError, TypeError):
                    raise TraceError("trajectory_export_invalid")
                if (not isinstance(event, dict) or len(event) > 128
                        or not set(event).issubset(EVENT_FIELDS)
                        or event.get("traceSchema") != TRAJECTORY_SCHEMA
                        or type(event.get("schemaVersion")) is not int
                        or event.get("schemaVersion") != TRAJECTORY_VERSION
                        or event.get("sessionId") != session_id
                        or event.get("source") not in ("runtime", "transcript")
                        or event.get("traceId") != trace_id
                        or not isinstance(event.get("type"), str)
                        or len(event["type"]) > 128):
                    raise TraceError("trajectory_export_invalid")
                if type(event.get("seq")) is not int or event["seq"] < 0:
                    raise TraceError("trajectory_export_invalid")
                if _iso_timestamp(event.get("ts")) is None:
                    raise TraceError("trajectory_export_invalid")
                if (event["source"] == "transcript"
                        and (type(event.get("sourceSeq")) is not int
                             or event["sourceSeq"] < 1)):
                    raise TraceError("trajectory_export_invalid")
                entry_id = event.get("entryId")
                if (entry_id is not None
                        and (not isinstance(entry_id, str) or not ID_RE.fullmatch(entry_id))):
                    raise TraceError("trajectory_export_invalid")
                public = _tool_event_from_native(event, session_id)
                if public is None:
                    continue
                event_id = _event_id(event)
                if event_id is None:
                    raise TraceError("trajectory_export_invalid")
                prior_native = seen_native_ids.get(event_id)
                if prior_native is not None:
                    if prior_native != public:
                        raise TraceError("trajectory_export_invalid")
                    continue
                seen_native_ids[event_id] = public
                if public.get("unidentified"):
                    continue
                kind, call_id = public["kind"], public["tool_call_id"]
                identity = (session_id, call_id, kind)
                prior = seen_tool_events.get(identity)
                if prior is not None:
                    if prior["name"] and public["name"] and prior["name"] != public["name"]:
                        raise TraceError("trajectory_export_invalid")
                    # A runtime event has stronger timing/result detail than the
                    # transcript alias, but both retain the native call id.
                    if public["source"] == "runtime" or prior["source"] != "runtime":
                        seen_tool_events[identity] = public
                else:
                    seen_tool_events[identity] = public
    except TraceError:
        raise
    except OSError:
        raise TraceError("trajectory_export_invalid")
    if parsed_line_count != declared_count:
        raise TraceError("trajectory_export_incomplete")
    return {
        "session_id": session_id,
        "session_key": _bounded_id(manifest.get("sessionKey")),
        "events": list(seen_tool_events.values()),
        "unidentified_tool_event_ids": {
            event_id for event_id, event in seen_native_ids.items()
            if event.get("unidentified")
        },
        "event_count": declared_count,
        "generated_at": _iso_timestamp(manifest.get("generatedAt")),
    }


def _tool_event_from_native(event, session_id):
    event_type = event.get("type")
    if event_type not in ("tool.call", "tool.result"):
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        raise TraceError("trajectory_export_invalid")
    message = data.get("message") if event.get("source") == "transcript" else None
    if message is not None and not isinstance(message, dict):
        raise TraceError("trajectory_export_invalid")
    call_id_value = data.get("toolCallId")
    if call_id_value is None and event_type == "tool.result" and isinstance(message, dict):
        call_id_value = message.get("toolCallId")
    call_id = _bounded_id(call_id_value)
    if call_id is None:
        _event_timestamp(event)
        return {"kind": event_type, "unidentified": True}
    name_value = data.get("name")
    if name_value is None and event_type == "tool.result" and isinstance(message, dict):
        name_value = message.get("toolName")
    name = _bounded_name(name_value)
    result = "unknown"
    if event_type == "tool.result":
        success = data.get("success")
        if success is None and isinstance(message, dict):
            is_error = message.get("isError")
            if is_error is not None and type(is_error) is not bool:
                raise TraceError("trajectory_export_invalid")
            if is_error is not None:
                success = not is_error
        if success is not None and type(success) is not bool:
            raise TraceError("trajectory_export_invalid")
        result = "succeeded" if success is True else "failed" if success is False else "unknown"
    return {
        "kind": event_type,
        "tool_call_id": call_id,
        "name": name,
        "at": _event_timestamp(event),
        "result": result,
        "source": event.get("source"),
        "run_id": _bounded_id(event.get("runId")),
        "session_id": session_id,
        "retry_context": "unknown",
    }


def _read_exports(root, *, deadline=None):
    if root is None:
        return {}, 0
    root = Path(root).expanduser()
    try:
        if root.is_symlink() or not root.is_dir():
            raise TraceError("trajectory_root_invalid")
        checkout = Path(__file__).resolve().parents[1]
        if os.path.commonpath((str(checkout), str(root.resolve()))) == str(checkout):
            raise TraceError("trajectory_root_invalid")
        directories = []
        for item in root.iterdir():
            if deadline is not None and time.monotonic() > deadline:
                raise TraceError("collection_timeout")
            if item.is_symlink() or not item.is_dir():
                continue
            directories.append(item)
            if len(directories) > EXPORT_COUNT_MAX:
                raise TraceError("trajectory_export_limit")
    except TraceError:
        raise
    except OSError:
        raise TraceError("trajectory_root_invalid")
    directories.sort(key=lambda item: item.name)
    sessions = {}
    total_bytes = 0
    total_events = 0
    for directory in directories:
        if deadline is not None and time.monotonic() > deadline:
            raise TraceError("collection_timeout")
        manifest_path = directory / "manifest.json"
        events_path = directory / "events.jsonl"
        if manifest_path.is_symlink() or events_path.is_symlink():
            raise TraceError("trajectory_export_invalid")
        if not manifest_path.exists() and not events_path.exists():
            continue
        try:
            total_bytes += manifest_path.stat().st_size + events_path.stat().st_size
        except OSError:
            raise TraceError("trajectory_export_invalid")
        if total_bytes > EXPORT_TOTAL_BYTES_MAX:
            raise TraceError("trajectory_export_limit")
        export = _parse_export_directory(directory, deadline=deadline)
        total_events += export["event_count"]
        if total_events > EVENT_TOTAL_COUNT_MAX:
            raise TraceError("trajectory_event_limit")
        session = sessions.setdefault(export["session_id"], {
            "session_key": export["session_key"], "events": {},
            "unidentified_tool_event_ids": set(),
            "event_count": 0, "export_count": 0, "generated_at": export["generated_at"],
        })
        session["export_count"] += 1
        session["event_count"] = max(session["event_count"], export["event_count"])
        if export["generated_at"] and (not session["generated_at"]
                                        or export["generated_at"] > session["generated_at"]):
            session["generated_at"] = export["generated_at"]
        for event in export["events"]:
            key = (event["tool_call_id"], event["kind"])
            old = session["events"].get(key)
            if old is None or event["source"] == "runtime" or old["source"] != "runtime":
                session["events"][key] = event
        session["unidentified_tool_event_ids"].update(export["unidentified_tool_event_ids"])
    return sessions, len(directories)


def _attach_tool_events(runs, sessions):
    by_run_id = {run["run_id"]: run for run in runs if run["run_id"]}
    by_session = {}
    for run in runs:
        if run["session_id"]:
            by_session.setdefault(run["session_id"], []).append(run)
    unjoined = 0
    ambiguous_sessions = set()
    unknown_join_sessions = set()
    unknown_join_runs = set()
    for session_id, session in sessions.items():
        unidentified_count = len(session.get("unidentified_tool_event_ids", set()))
        if unidentified_count:
            unjoined += unidentified_count
            unknown_join_sessions.add(session_id)
            for candidate in by_session.get(session_id, []):
                candidate["attention_codes"].append("tool_join_unknown")
    for session_id, session in sessions.items():
        candidates = by_session.get(session_id, [])
        for event in session["events"].values():
            target = None
            if event["run_id"]:
                target = by_run_id.get(event["run_id"])
                if (target and target["session_id"] and event["session_id"]
                        and target["session_id"] != event["session_id"]):
                    unknown_join_runs.add(target["run_id"])
                    target["attention_codes"].append("tool_join_unknown")
                    target = None
                if target is None and candidates:
                    unknown_join_sessions.add(session_id)
                    for candidate in candidates:
                        candidate["attention_codes"].append("tool_join_unknown")
            elif len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1:
                ambiguous_sessions.add(session_id)
                for candidate in candidates:
                    candidate["tool_trace_status"] = "partial"
                    candidate["attention_codes"].append("session_join_ambiguous")
            if target is None:
                unjoined += 1
                continue
            target["tool_events"].append(event)
    for run in runs:
        calls = {}
        results = {}
        for event in run["tool_events"]:
            if event["kind"] == "tool.call":
                calls[event["tool_call_id"]] = event
            else:
                results[event["tool_call_id"]] = event
        tool_events = []
        missing_result = False
        for call_id, call in sorted(calls.items()):
            result = results.get(call_id)
            start_at = call["at"]
            end_at = result["at"] if result else None
            duration = None
            if start_at and end_at:
                delta = _timestamp_ms(end_at) - _timestamp_ms(start_at)
                if delta >= 0:
                    duration = delta
            if result is None:
                missing_result = True
            elif result["result"] == "unknown":
                missing_result = True
            tool_events.append({
                "tool_call_id": call_id,
                "name": call["name"] or (result["name"] if result else None),
                "started_at": start_at,
                "finished_at": end_at,
                "duration_ms": duration,
                "result": result["result"] if result else "unknown",
                "retry_context": call["retry_context"],
            })
        for call_id, result in results.items():
            if call_id not in calls:
                tool_events.append({
                    "tool_call_id": call_id, "name": result["name"],
                    "started_at": None, "finished_at": result["at"],
                    "duration_ms": None, "result": result["result"],
                    "retry_context": result["retry_context"],
                })
                missing_result = True
        run["tool_events"] = sorted(tool_events, key=lambda item: (item["started_at"] or "", item["tool_call_id"]))
        if run["run_id"] in unknown_join_runs:
            run["tool_trace_status"] = "partial"
        elif run["session_id"] in ambiguous_sessions:
            run["tool_trace_status"] = "partial"
            run["attention_codes"].append("session_join_ambiguous")
        elif run["session_id"] in sessions:
            incomplete_join = run["session_id"] in unknown_join_sessions
            run["tool_trace_status"] = "partial" if missing_result or incomplete_join else "complete"
            if missing_result:
                run["attention_codes"].append("tool_trace_partial")
        else:
            run["tool_trace_status"] = "missing"
            run["attention_codes"].append("tool_trace_missing")
        run["attention_codes"] = sorted(set(run["attention_codes"]))
    return unjoined


def _retention_limit(value=None):
    if value is None:
        raw = os.environ.get("JOBSEARCH_TRACE_RETENTION_RECORDS", str(RETENTION_DEFAULT))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise TraceError("configuration_invalid")
    if type(value) is not int or value < 1 or value > RETENTION_MAX:
        raise TraceError("configuration_invalid")
    return value


def collect_native(runner=None, *, trajectory_root=None, page_size=PAGE_SIZE_MAX,
                   max_pages=PAGE_COUNT_MAX, retention_records=None,
                   timeout_seconds=COLLECTION_TIMEOUT_SECONDS, binary=None):
    """Read a stable bounded page set plus local supported trajectory exports."""
    runner = runner or subprocess.run
    try:
        retention = _retention_limit(retention_records)
    except TraceError as exc:
        return {"records": None, "observed_at": None, "error_code": exc.code}
    if (type(page_size) is not int or page_size < 1 or page_size > PAGE_SIZE_MAX
            or type(max_pages) is not int or max_pages < 1 or max_pages > PAGE_COUNT_MAX
            or type(timeout_seconds) not in (int, float) or timeout_seconds <= 0
            or timeout_seconds > COLLECTION_TIMEOUT_SECONDS):
        return {"records": None, "observed_at": None, "error_code": "configuration_invalid"}
    binary = binary if binary is not None else os.environ.get("JOBSEARCH_OPENCLAW_BIN", "openclaw")
    if not isinstance(binary, str) or not binary.strip() or "\0" in binary or len(binary) > 512:
        return {"records": None, "observed_at": None, "error_code": "configuration_invalid"}
    binary = binary.strip()
    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        version_text = _invoke(runner, [binary, "--version"],
                               timeout=min(COMMAND_TIMEOUT_SECONDS, timeout_seconds))
        if not _version_supported(version_text):
            raise TraceError("unsupported_version")
        if time.monotonic() > deadline:
            raise TraceError("collection_timeout")
        first, first_pages, total, first_bytes = _read_page_pass(
            runner, binary=binary, page_size=page_size, max_pages=max_pages, deadline=deadline)
        # OpenClaw's run-history page has no revision token. Repeat the full
        # bounded read and compare native page identities before publishing.
        second, second_pages, second_total, _second_bytes = _read_page_pass(
            runner, binary=binary, page_size=page_size, max_pages=max_pages,
            deadline=deadline, byte_budget=CLI_COLLECTION_BYTES_MAX - first_bytes)
        if first_pages != second_pages or total != second_total:
            raise TraceError("snapshot_changed")
        runs = _dedupe_runs(first)
        sessions, export_count = _read_exports(trajectory_root, deadline=deadline)
        unjoined = _attach_tool_events(runs, sessions)
        runs.sort(key=lambda row: (row["_finished_ms"], row["run_id"] or ""), reverse=True)
        if time.monotonic() > deadline:
            raise TraceError("collection_timeout")
        dropped = max(0, len(runs) - retention)
        retained_runs = runs[:retention]
        if dropped:
            for run in retained_runs:
                run["attention_codes"].append("retention_truncated")
                run["attention_codes"] = sorted(set(run["attention_codes"]))
        records = [{
            "kind": "collection",
            "native_version": NATIVE_VERSION,
            "native_commit": NATIVE_COMMIT,
            "native_total": total,
            "retention_limit": retention,
            "retained_run_count": len(retained_runs),
            "dropped_run_count": dropped,
            "trajectory_export_count": export_count,
            "trajectory_session_count": len(sessions),
            "unjoined_tool_event_count": unjoined,
            "cursor_kind": "cron_runs_offset",
            "next_offset": total,
            "complete": True,
        }]
        records.extend(_run_public(row) for row in retained_runs)
        return {"records": records, "observed_at": None, "error_code": None}
    except TraceError as exc:
        return {"records": None, "observed_at": None, "error_code": exc.code}
    except Exception:
        # Source text, stderr, arguments, and local paths are never surfaced.
        return {"records": None, "observed_at": None, "error_code": "source_unavailable"}


def sanitize_collection(value):
    """Rebuild a strict projection payload from adapter output."""
    if not isinstance(value, dict) or set(value) - {"records", "observed_at", "error_code"}:
        return {"records": None, "observed_at": None, "error_code": "source_invalid"}
    records = value.get("records")
    if records is None:
        code = value.get("error_code")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            code = "source_invalid"
        return {"records": None, "observed_at": None, "error_code": code}
    if not isinstance(records, list) or len(records) > RUN_COUNT_MAX + 1:
        return {"records": None, "observed_at": None, "error_code": "source_invalid"}
    clean = []
    try:
        collection = None
        runs = []
        for record in records:
            if not isinstance(record, dict):
                raise TraceError("source_invalid")
            if record.get("kind") == "collection":
                keys = {"kind", "native_version", "native_commit", "native_total", "retention_limit",
                        "retained_run_count", "dropped_run_count", "trajectory_export_count",
                        "trajectory_session_count", "unjoined_tool_event_count", "cursor_kind",
                        "next_offset", "complete"}
                if collection is not None or not set(record).issubset(keys):
                    raise TraceError("source_invalid")
                collection = dict(record)
            elif record.get("kind") == "run":
                runs.append(record)
            else:
                raise TraceError("source_invalid")
        if collection is None or collection.get("complete") is not True:
            raise TraceError("source_invalid")
        for key in ("native_total", "retention_limit", "retained_run_count", "dropped_run_count",
                    "trajectory_export_count", "trajectory_session_count", "unjoined_tool_event_count",
                    "next_offset"):
            _integer(collection.get(key), optional=False, maximum=RUN_COUNT_MAX)
        if (collection["retention_limit"] < 1 or collection["retention_limit"] > RETENTION_MAX
                or collection["retained_run_count"] > collection["retention_limit"]
                or collection["native_total"] < collection["retained_run_count"] + collection["dropped_run_count"]
                or collection["next_offset"] != collection["native_total"]
                or collection["trajectory_export_count"] > EXPORT_COUNT_MAX
                or collection["trajectory_session_count"] > collection["trajectory_export_count"]
                or collection["unjoined_tool_event_count"] > EVENT_TOTAL_COUNT_MAX):
            raise TraceError("source_invalid")
        if collection.get("native_version") != NATIVE_VERSION or collection.get("native_commit") != NATIVE_COMMIT:
            raise TraceError("source_invalid")
        if collection.get("cursor_kind") != "cron_runs_offset":
            raise TraceError("source_invalid")
        if collection["retained_run_count"] != len(runs):
            raise TraceError("source_invalid")
        for run in runs:
            keys = {"kind", "run_id", "job_id", "session_id", "session_key", "scheduler_status",
                    "execution_result", "completion_status", "business_result",
                    "application_join_status", "artifact_join_status", "delivery_status",
                    "trigger_fired", "started_at", "finished_at", "duration_ms", "provider", "model",
                    "usage", "tool_trace_status", "tool_events", "retry_context", "attention_codes"}
            if not set(run).issubset(keys):
                raise TraceError("source_invalid")
            _bounded_id(run.get("run_id"))
            _bounded_id(run.get("job_id"), required=True)
            _bounded_id(run.get("session_id"))
            _bounded_id(run.get("session_key"))
            scheduler_status = run.get("scheduler_status")
            if scheduler_status is not None and scheduler_status not in STATUS_VALUES:
                raise TraceError("source_invalid")
            for field, values in (("execution_result", ("succeeded", "failed", "skipped", "interrupted", "unknown")),
                                  ("completion_status", tuple(COMPLETION_VALUES)),
                                  ("business_result", ("unknown", "no_work")),
                                  ("application_join_status", ("unknown",)),
                                  ("artifact_join_status", ("unknown",)),
                                  ("delivery_status", ("delivered", "not_requested", "suppressed", "not_delivered", "unknown")),
                                  ("tool_trace_status", ("missing", "partial", "complete")),
                                  ("retry_context", ("unknown",))):
                if run.get(field) not in values:
                    raise TraceError("source_invalid")
            if run.get("trigger_fired") is not None and type(run.get("trigger_fired")) is not bool:
                raise TraceError("source_invalid")
            for field in ("started_at", "finished_at"):
                if run.get(field) is not None and _iso_timestamp(run[field]) is None:
                    raise TraceError("source_invalid")
            _integer(run.get("duration_ms"), maximum=10 ** 12)
            provider = _bounded_name(run.get("provider"))
            model = _bounded_name(run.get("model"))
            usage = run.get("usage")
            safe_usage = None
            if usage is not None:
                usage_keys = {"scope", "provider", "input_tokens", "output_tokens", "cache_read_tokens",
                              "cache_write_tokens", "cost_status"}
                if not isinstance(usage, dict) or not set(usage).issubset(usage_keys):
                    raise TraceError("source_invalid")
                if usage.get("scope") != "cron_run" or usage.get("cost_status") != "unavailable":
                    raise TraceError("source_invalid")
                safe_usage = {"scope": "cron_run", "provider": provider,
                              "input_tokens": _safe_counter(usage.get("input_tokens")),
                              "output_tokens": _safe_counter(usage.get("output_tokens")),
                              "cache_read_tokens": _safe_counter(usage.get("cache_read_tokens")),
                              "cache_write_tokens": _safe_counter(usage.get("cache_write_tokens")),
                              "cost_status": "unavailable"}
            tool_rows = run.get("tool_events")
            if not isinstance(tool_rows, list) or len(tool_rows) > EVENT_COUNT_MAX:
                raise TraceError("source_invalid")
            safe_tools = []
            for tool in tool_rows:
                tool_keys = {"tool_call_id", "name", "started_at", "finished_at", "duration_ms",
                             "result", "retry_context"}
                if not isinstance(tool, dict) or not set(tool).issubset(tool_keys):
                    raise TraceError("source_invalid")
                tool_result = tool.get("result")
                if tool_result not in ("succeeded", "failed", "unknown"):
                    raise TraceError("source_invalid")
                tool_started = tool.get("started_at")
                tool_finished = tool.get("finished_at")
                if ((tool_started is not None and _iso_timestamp(tool_started) is None)
                        or (tool_finished is not None and _iso_timestamp(tool_finished) is None)):
                    raise TraceError("source_invalid")
                safe_tools.append({
                    "tool_call_id": _bounded_id(tool.get("tool_call_id"), required=True),
                    "name": _bounded_name(tool.get("name")),
                    "started_at": _iso_timestamp(tool_started) if tool_started else None,
                    "finished_at": _iso_timestamp(tool_finished) if tool_finished else None,
                    "duration_ms": _integer(tool.get("duration_ms"), maximum=10 ** 12),
                    "result": tool_result,
                    "retry_context": "unknown",
                })
            attention = run.get("attention_codes")
            valid_attention = {"missing_completion", "scheduler_failed", "scheduler_interrupted", "completion_failed", "planned_skip",
                               "completion_unknown", "business_result_unknown", "delivery_unknown",
                               "run_id_unknown", "session_join_unknown", "session_join_ambiguous",
                               "tool_trace_missing", "tool_trace_partial", "tool_join_unknown",
                               "retention_truncated"}
            if (not isinstance(attention, list) or len(attention) > 32
                    or any(code not in valid_attention for code in attention)):
                raise TraceError("source_invalid")
            clean.append({
                "kind": "run", "run_id": run.get("run_id"), "job_id": run["job_id"],
                "session_id": run.get("session_id"), "session_key": run.get("session_key"),
                "scheduler_status": run.get("scheduler_status"),
                "execution_result": run["execution_result"], "completion_status": run["completion_status"],
                "business_result": run["business_result"],
                "application_join_status": run["application_join_status"],
                "artifact_join_status": run["artifact_join_status"],
                "delivery_status": run["delivery_status"],
                "trigger_fired": run.get("trigger_fired"), "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"), "duration_ms": run.get("duration_ms"),
                "provider": provider, "model": model, "usage": safe_usage,
                "tool_trace_status": run["tool_trace_status"], "tool_events": safe_tools,
                "retry_context": "unknown", "attention_codes": sorted(set(attention)),
            })
        clean.insert(0, collection)
        return {"records": clean, "observed_at": None, "error_code": None}
    except (TraceError, TypeError, ValueError, KeyError):
        return {"records": None, "observed_at": None, "error_code": "source_invalid"}


def collect_and_project(*, db_path=None, trajectory_root=None, runner=None,
                        page_size=PAGE_SIZE_MAX, max_pages=PAGE_COUNT_MAX,
                        retention_records=None, as_of=None, binary=None):
    """Collect native source facts then publish through the accepted projection."""
    data = collect_native(runner, trajectory_root=trajectory_root,
                          page_size=page_size, max_pages=max_pages,
                          retention_records=retention_records, binary=binary)
    try:
        import projection
        return projection.collect_projection(db_path=db_path, as_of=as_of, trace_data=data)
    except Exception:
        # No source output or exception text is safe to echo here.
        return {"published": False, "error_code": "projection_unavailable"}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="explicitly read native cron runs and private trajectory exports")
    collect.add_argument("--db", default=None)
    collect.add_argument("--trajectory-root", default=os.environ.get("JOBSEARCH_OPENCLAW_TRAJECTORY_ROOT"))
    collect.add_argument("--openclaw-bin", default=os.environ.get("JOBSEARCH_OPENCLAW_BIN", "openclaw"))
    collect.add_argument("--page-size", type=int, default=PAGE_SIZE_MAX)
    collect.add_argument("--max-pages", type=int, default=PAGE_COUNT_MAX)
    collect.add_argument("--retention-records", type=int, default=None)
    collect.add_argument("--as-of", default=None)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "collect":
        result = collect_and_project(
            db_path=args.db, trajectory_root=args.trajectory_root,
            page_size=args.page_size, max_pages=args.max_pages,
            retention_records=args.retention_records, as_of=args.as_of,
            binary=args.openclaw_bin)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("published") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
