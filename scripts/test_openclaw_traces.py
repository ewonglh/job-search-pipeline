#!/usr/bin/env python3
"""Synthetic checks for the versioned OpenClaw run and trajectory adapter."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
from datetime import datetime, timezone

import openclaw_traces
import projection


CANARIES = (
    "TRACE-SECRET-CANARY", "TRACE-OTP-654321", "TRACE-MAIL-CANARY",
    "TRACE-ARGUMENT-CANARY", "TRACE-OUTPUT-CANARY", "TRACE-PROMPT-CANARY",
)
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)


def _run(run_id, job_id, status, *, ts, session_id=None, **extra):
    value = {"runId": run_id, "jobId": job_id, "action": "finished",
             "status": status, "ts": ts, "runAtMs": ts - 1000,
             "durationMs": 1000}
    if session_id is not None:
        value["sessionId"] = session_id
    value.update(extra)
    return value


def _page(entries, offset, limit):
    total = len(entries)
    rows = entries[offset:offset + limit]
    next_offset = offset + len(rows)
    has_more = next_offset < total
    return {"entries": rows, "total": total, "offset": offset, "limit": limit,
            "hasMore": has_more, "nextOffset": next_offset if has_more else None}


class FakeRunner:
    def __init__(self, entries, *, fail_offset=None, change_second_pass=False):
        self.entries = entries
        self.fail_offset = fail_offset
        self.change_second_pass = change_second_pass
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv == ["openclaw", "--version"]:
            return SimpleNamespace(returncode=0, stdout="OpenClaw 2026.10.1 (e178c39)\n")
        offset = int(argv[argv.index("--offset") + 1])
        limit = int(argv[argv.index("--limit") + 1])
        if self.fail_offset == offset and self.calls.count(argv) == 1:
            raise subprocess.TimeoutExpired(argv, 1)
        entries = list(self.entries)
        run_count = len([call for call in self.calls if "--offset" in call])
        if self.change_second_pass and run_count > 2 and entries:
            entries[0] = dict(entries[0], status="error", completionStatus="failed")
        return SimpleNamespace(returncode=0, stdout=json.dumps(_page(entries, offset, limit)))


def _event(source, event_type, *, seq, session_id="session-1", data=None,
           entry_id=None, source_seq=None, run_id=None):
    value = {"traceSchema": "openclaw-trajectory", "schemaVersion": 1,
             "traceId": session_id, "source": source, "type": event_type,
             "ts": "2026-11-16T07:59:00Z", "seq": seq,
             "sessionId": session_id, "data": data or {}}
    if entry_id is not None:
        value["entryId"] = entry_id
    if source_seq is not None:
        value["sourceSeq"] = source_seq
    if run_id is not None:
        value["runId"] = run_id
    return value


def _write_export(root, name, events, *, session_id="session-1", version=1):
    directory = root / name
    directory.mkdir(parents=True)
    manifest = {"traceSchema": "openclaw-trajectory", "schemaVersion": version,
                "traceId": session_id, "sessionId": session_id,
                "sessionKey": "agent:main:test",
                "generatedAt": "2026-11-16T08:00:00Z", "eventCount": len(events)}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "events.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events), encoding="utf-8")
    return directory


def _trace_paths(root):
    root.mkdir(parents=True)
    repo = root / "repo"
    runtime = root / "runtime"
    repo.mkdir()
    runtime.mkdir()
    tracker = repo / "applications.yaml"
    queue = runtime / "queue.yaml"
    events = runtime / "events.json"
    ingestion = runtime / "ingestion.json"
    snapshots = runtime / "snapshots"
    reviews = runtime / "reviews.json"
    preflight = runtime / "preflight.json"
    tracker.write_text("[]\n", encoding="utf-8")
    queue.write_text("[]\n", encoding="utf-8")
    events.write_text(json.dumps({"schema": 1, "coverage": {"complete": True}, "events": []}),
                      encoding="utf-8")
    ingestion.write_text(json.dumps({"schema": 1, "scopes": {}}), encoding="utf-8")
    snapshots.mkdir()
    reviews.write_text(json.dumps({"reviewed": {}}), encoding="utf-8")
    preflight.write_text(json.dumps({"schema": 1, "records": []}), encoding="utf-8")
    return {"repo": str(repo), "tracker": str(tracker), "queue": str(queue),
            "ledger": str(events), "ingestion": str(ingestion),
            "snapshot_root": str(snapshots), "review_state": str(reviews),
            "preflight_state": str(preflight)}


success = _run("run-1", "job-1", "ok", ts=1794815940000, session_id="session-1",
               completionStatus="succeeded", deliveryStatus="not-requested",
               delivered=False, triggerFired=True, provider="provider-a", model="model-a",
               usage={"input_tokens": 17, "output_tokens": 5,
                      "cache_read_tokens": 3, "cache_write_tokens": 1,
                      "total_tokens": 25},
               summary=CANARIES[5], error=CANARIES[0])
failure = _run("run-2", "job-2", "error", ts=1794815941000, session_id="session-2",
               completionStatus="failed", deliveryStatus="unknown",
               error=CANARIES[3], deliveryError=CANARIES[2])
interrupted = _run("run-3", "job-3", "error", ts=1794815942000,
                   completionStatus="failed", error=openclaw_traces.INTERRUPTED_ERROR)
skipped = _run("run-4", "job-4", "skipped", ts=1794815943000,
               completionStatus="failed", deliveryStatus="not-requested",
               triggerFired=False, summary=CANARIES[1])
silent = _run("run-5", "job-5", "ok", ts=1794815944000, session_id="session-5",
              completionStatus="succeeded", deliveryStatus="not-requested",
              delivered=False, triggerFired=True)
entries = [success, failure, interrupted, skipped, silent]

tool_call_id = "tool-call-1"
trajectory_events = [
    _event("transcript", "assistant.message", seq=0, entry_id="entry-1", source_seq=1,
           data={"message": CANARIES[5]}),
    _event("transcript", "tool.call", seq=0, entry_id="entry-1", source_seq=2,
           data={"toolCallId": tool_call_id, "name": "browser.open",
                 "arguments": CANARIES[3]}),
    _event("runtime", "tool.call", seq=2,
           data={"toolCallId": tool_call_id, "name": "browser.open",
                 "args": {"otp": CANARIES[1], "mail": CANARIES[2]}}),
    _event("runtime", "tool.result", seq=3,
           data={"toolCallId": tool_call_id, "name": "browser.open",
                 "success": True, "result": CANARIES[4]}),
]

with tempfile.TemporaryDirectory(prefix="synthetic-openclaw-traces-") as temp:
    root = Path(temp)
    exports = root / "exports"
    exports.mkdir()
    _write_export(exports, "export-a", trajectory_events)
    _write_export(exports, "export-alias", trajectory_events)
    runner = FakeRunner(entries)
    result = openclaw_traces.collect_native(
        runner, trajectory_root=exports, page_size=2, retention_records=10)
    assert result["error_code"] is None, result
    assert [call[-1] for call in runner.calls if "--offset" in call] == ["0", "2", "4", "0", "2", "4"]
    records = result["records"]
    runs = {row["run_id"]: row for row in records if row["kind"] == "run"}
    assert records[0]["native_total"] == 5
    assert runs["run-1"]["tool_trace_status"] == "complete"
    assert len(runs["run-1"]["tool_events"]) == 1
    assert runs["run-1"]["tool_events"][0]["result"] == "succeeded"
    assert runs["run-1"]["tool_events"][0]["duration_ms"] == 0
    assert runs["run-1"]["business_result"] == "unknown"
    assert runs["run-1"]["application_join_status"] == "unknown"
    assert runs["run-1"]["artifact_join_status"] == "unknown"
    assert runs["run-1"]["delivery_status"] == "not_requested"
    assert runs["run-1"]["usage"]["scope"] == "cron_run"
    assert runs["run-1"]["usage"]["input_tokens"] == 17
    assert "total_tokens" not in runs["run-1"]["usage"]
    assert runs["run-1"]["usage"]["cost_status"] == "unavailable"
    assert runs["run-2"]["execution_result"] == "failed"
    assert runs["run-3"]["execution_result"] == "interrupted"
    assert "scheduler_interrupted" in runs["run-3"]["attention_codes"]
    assert "session_join_unknown" in runs["run-3"]["attention_codes"]
    assert runs["run-4"]["execution_result"] == "skipped"
    assert runs["run-4"]["completion_status"] == "failed"
    assert runs["run-4"]["business_result"] == "no_work"
    assert runs["run-4"]["delivery_status"] == "not_requested"
    assert "planned_skip" in runs["run-4"]["attention_codes"]
    assert "scheduler_failed" not in runs["run-4"]["attention_codes"]
    assert "completion_failed" not in runs["run-4"]["attention_codes"]
    assert runs["run-5"]["tool_trace_status"] == "missing"
    assert "business_result_unknown" in runs["run-5"]["attention_codes"]
    exported = json.dumps(records, sort_keys=True)
    assert all(canary not in exported for canary in CANARIES)

    # Transcript tool results carry their fields inside data.message, while
    # content remains private. An event without a call ID becomes a visible gap.
    transcript_exports = root / "transcript"
    transcript_exports.mkdir()
    transcript_tool_events = [
        _event("transcript", "tool.call", seq=1, entry_id="entry-call", source_seq=1,
               data={"toolCallId": "tool-transcript", "name": "files.read",
                     "arguments": CANARIES[3]}),
        _event("transcript", "tool.result", seq=2, entry_id="entry-result", source_seq=1,
               data={"message": {"role": "toolResult", "toolCallId": "tool-transcript",
                                  "toolName": "files.read", "isError": True,
                                  "content": CANARIES[4]}}),
    ]
    _write_export(transcript_exports, "export", transcript_tool_events)
    transcript_result = openclaw_traces.collect_native(
        FakeRunner([success]), trajectory_root=transcript_exports, page_size=5)
    transcript_run = next(row for row in transcript_result["records"] if row["kind"] == "run")
    assert transcript_run["tool_trace_status"] == "complete"
    assert transcript_run["tool_events"][0]["result"] == "failed"
    assert all(canary not in json.dumps(transcript_result) for canary in CANARIES)

    unidentified_exports = root / "unidentified"
    unidentified_exports.mkdir()
    unidentified_event = _event("runtime", "tool.call", seq=4,
                                 data={"name": "files.read", "args": CANARIES[2]})
    _write_export(unidentified_exports, "export", [unidentified_event])
    unidentified = openclaw_traces.collect_native(
        FakeRunner([success]), trajectory_root=unidentified_exports, page_size=5)
    unidentified_run = next(row for row in unidentified["records"] if row["kind"] == "run")
    assert unidentified_run["tool_trace_status"] == "partial"
    assert "tool_join_unknown" in unidentified_run["attention_codes"]
    assert unidentified["records"][0]["unjoined_tool_event_count"] == 1

    # The supported page shape has no revision token. An unrecognized page
    # field fails closed instead of making a fixture-only cursor API.
    page_runner = FakeRunner(entries)
    def extra_page_field(argv, **kwargs):
        result = page_runner(argv, **kwargs)
        if "--offset" in argv:
            page = json.loads(result.stdout)
            page["snapshotRevision"] = "fixture-only"
            result.stdout = json.dumps(page)
        return result
    unsupported_page = openclaw_traces.collect_native(extra_page_field, page_size=2)
    assert unsupported_page["error_code"] == "unsupported_schema"

    original_cli_budget = openclaw_traces.CLI_COLLECTION_BYTES_MAX
    openclaw_traces.CLI_COLLECTION_BYTES_MAX = 32
    try:
        oversized_pages = openclaw_traces.collect_native(FakeRunner(entries), page_size=2)
        assert oversized_pages["error_code"] == "native_output_too_large"
    finally:
        openclaw_traces.CLI_COLLECTION_BYTES_MAX = original_cli_budget

    missing_completion = openclaw_traces.collect_native(
        FakeRunner([_run("run-missing", "job-missing", None, ts=1794815946000)]),
        page_size=1)
    missing_run = next(row for row in missing_completion["records"] if row["kind"] == "run")
    assert missing_run["execution_result"] == "unknown"
    assert "missing_completion" in missing_run["attention_codes"]

    ambiguous_rows = [
        _run("run-a", "job-a", "ok", ts=1794815947000, session_id="session-1"),
        _run("run-b", "job-b", "ok", ts=1794815948000, session_id="session-1"),
    ]
    ambiguous = openclaw_traces.collect_native(
        FakeRunner(ambiguous_rows), trajectory_root=exports, page_size=5)
    ambiguous_runs = [row for row in ambiguous["records"] if row["kind"] == "run"]
    assert all(row["tool_trace_status"] == "partial" for row in ambiguous_runs)
    assert all("session_join_ambiguous" in row["attention_codes"] for row in ambiguous_runs)
    assert ambiguous["records"][0]["unjoined_tool_event_count"] > 0

    # An explicit but unmatched run ID blocks a weaker session-only join and
    # marks the affected run as partial instead of reporting an empty complete trace.
    mismatch_exports = root / "mismatch"
    mismatch_exports.mkdir()
    mismatch_events = [
        _event("runtime", "tool.call", seq=1, run_id="run-elsewhere",
               data={"toolCallId": "tool-mismatch", "name": "browser.open"}),
    ]
    _write_export(mismatch_exports, "export", mismatch_events)
    mismatch = openclaw_traces.collect_native(
        FakeRunner([success]), trajectory_root=mismatch_exports, page_size=5)
    mismatch_run = next(row for row in mismatch["records"] if row["kind"] == "run")
    assert mismatch_run["tool_trace_status"] == "partial"
    assert "tool_join_unknown" in mismatch_run["attention_codes"]
    assert mismatch["records"][0]["unjoined_tool_event_count"] == 1

    # An interrupted page pass does not publish its partial list. A new bounded
    # collection restarts at offset zero and obtains every page before publish.
    interrupted_runner = FakeRunner(entries, fail_offset=2)
    failed = openclaw_traces.collect_native(interrupted_runner, page_size=2)
    assert failed["records"] is None and failed["error_code"] == "native_timeout"
    restarted_runner = FakeRunner(entries)
    restarted = openclaw_traces.collect_native(restarted_runner, page_size=2)
    assert restarted["error_code"] is None
    assert restarted["records"][0]["native_total"] == 5
    assert restarted_runner.calls[1][-1] == "0"

    # A moving offset inventory and a malformed entry cannot advance the source.
    changing = openclaw_traces.collect_native(
        FakeRunner(entries, change_second_pass=True), page_size=2)
    assert changing["records"] is None and changing["error_code"] == "snapshot_changed"
    malformed_runner = FakeRunner([dict(success, unexpected=CANARIES[4])])
    malformed = openclaw_traces.collect_native(malformed_runner, page_size=2)
    assert malformed["records"] is None and malformed["error_code"] == "unsupported_schema"
    limited = openclaw_traces.collect_native(FakeRunner(entries), page_size=2, max_pages=1)
    assert limited["records"] is None and limited["error_code"] == "page_limit"
    retained = openclaw_traces.collect_native(
        FakeRunner(entries), page_size=2, retention_records=2)
    assert retained["error_code"] is None
    assert retained["records"][0]["dropped_run_count"] == 3
    assert len([row for row in retained["records"] if row["kind"] == "run"]) == 2
    assert all("retention_truncated" in row["attention_codes"]
               for row in retained["records"] if row["kind"] == "run")

    unsupported = openclaw_traces.collect_native(
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="OpenClaw 2026.11.0 (abcdef0)"),
        page_size=1)
    assert unsupported["error_code"] == "unsupported_version"

    def slow_version(argv, **kwargs):
        time.sleep(0.01)
        return SimpleNamespace(returncode=0, stdout="OpenClaw 2026.10.1 (e178c39)")
    timed_out = openclaw_traces.collect_native(slow_version, page_size=1,
                                               timeout_seconds=0.001)
    assert timed_out["error_code"] == "collection_timeout"

    original_export_budget = openclaw_traces.EXPORT_TOTAL_BYTES_MAX
    first_export = exports / "export-a"
    openclaw_traces.EXPORT_TOTAL_BYTES_MAX = (
        (first_export / "manifest.json").stat().st_size
        + (first_export / "events.jsonl").stat().st_size + 1)
    try:
        over_budget_exports = openclaw_traces.collect_native(
            FakeRunner(entries), trajectory_root=exports, page_size=5)
        assert over_budget_exports["error_code"] == "trajectory_export_limit"
    finally:
        openclaw_traces.EXPORT_TOTAL_BYTES_MAX = original_export_budget

    # A supported schema 1 export is read as a stream and aliases deduplicate
    # by tool call ID. Unsupported versions fail closed without source content.
    bad_exports = root / "unsupported"
    bad_exports.mkdir()
    _write_export(bad_exports, "bad", trajectory_events, version=2)
    bad_trace = openclaw_traces.collect_native(FakeRunner(entries), trajectory_root=bad_exports)
    assert bad_trace["records"] is None and bad_trace["error_code"] == "unsupported_trajectory_version"
    unknown_field_exports = root / "unknown-field"
    unknown_field_exports.mkdir()
    unknown_event = dict(trajectory_events[1], privateField=CANARIES[0])
    _write_export(unknown_field_exports, "bad", [unknown_event])
    unknown_field = openclaw_traces.collect_native(
        FakeRunner(entries), trajectory_root=unknown_field_exports)
    assert unknown_field["records"] is None
    assert unknown_field["error_code"] == "trajectory_export_invalid"
    in_repo = openclaw_traces.collect_native(
        FakeRunner(entries), trajectory_root=Path(__file__).resolve().parents[1])
    assert in_repo["records"] is None and in_repo["error_code"] == "trajectory_root_invalid"

    # Projection accepts only the re-sanitized adapter contract, retains
    # last-good data on collection failure, and exposes a read-only drilldown.
    paths = _trace_paths(root / "projection")
    db_path = root / "projection" / "private" / "index.sqlite3"
    collected = projection.collect_projection(
        db_path=str(db_path), as_of="2026-11-16", now=NOW,
        trace_data=result, **paths)
    assert collected["published"] is True
    snapshot = projection.read_snapshot(str(db_path))
    assert snapshot["sources"]["traces"]["status"] == "stale"
    assert snapshot["sources"]["traces"]["observed_at"] is None
    assert snapshot["facts"]["traces"]["runs"]
    report = projection.build_trace_report(snapshot)
    report_text = json.dumps(report, sort_keys=True)
    assert all(canary not in report_text for canary in CANARIES)
    assert all(canary not in b"".join(path.read_bytes() for path in db_path.parent.glob("index.sqlite3*"))
               for canary in (value.encode() for value in CANARIES))
    failed_refresh = projection.collect_projection(
        db_path=str(db_path), as_of="2026-11-16", now=NOW,
        trace_data={"records": None, "error_code": "page_incomplete"}, **paths)
    assert failed_refresh["published"] is True
    after_failure = projection.read_snapshot(str(db_path))
    assert after_failure["sources"]["traces"]["status"] == "stale"
    assert after_failure["sources"]["traces"]["error_code"] == "page_incomplete"
    assert len(after_failure["facts"]["traces"]["runs"]) == len(snapshot["facts"]["traces"]["runs"])
    formatted = projection.format_traces(after_failure)
    assert "cost unavailable" in formatted
    assert "application_join=unknown artifact_join=unknown" in formatted
    assert "tool-call-1" in formatted and "retry=unknown" in formatted
    assert "start=2026-11-16T07:59:00Z" in formatted

    # Existing projection generation cursors make sanitized trace records
    # resumable when publication stops between bounded write batches.
    changed_data = openclaw_traces.collect_native(
        FakeRunner(entries + [_run("run-6", "job-6", "ok", ts=1794815945000,
                                   triggerFired=True)]),
        page_size=2, retention_records=10)
    assert changed_data["error_code"] is None
    partial = projection.collect_projection(
        db_path=str(db_path), as_of="2026-11-16", now=NOW,
        batch_size=1, interrupt_after_batches=3, trace_data=changed_data, **paths)
    assert partial["published"] is False
    check = sqlite3.connect(str(db_path))
    try:
        staging = check.execute(
            "SELECT cursor,total FROM source_generations WHERE source='traces' AND state='staging'").fetchone()
        assert staging is not None
        assert 0 <= staging[0] <= staging[1]
    finally:
        check.close()
    resumed = projection.collect_projection(
        db_path=str(db_path), as_of="2026-11-16", now=NOW,
        batch_size=1, trace_data=changed_data, **paths)
    assert resumed["published"] is True

print("OpenClaw trace adapter synthetic checks passed")
