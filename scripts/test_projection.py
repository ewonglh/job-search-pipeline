#!/usr/bin/env python3
"""Synthetic offline checks for the shared read-only SQLite projection."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest import mock
import urllib.request

import funnel_report
import pipeline_health
import projection
import review_contract


NOW = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
CANARY = "PRIVATE-CANARY-I11-DO-NOT-PERSIST"
OWNER_NAME = "PRIVATE-OWNER-NAME-I11"
HEAD1 = "1" * 40
HEAD2 = "2" * 40


def _yaml(rows):
    if not rows:
        return "[]\n"
    lines = []
    for row in rows:
        lines.append("- company: \"{}\"".format(row.get("company", "Example Co")))
        for key, value in row.items():
            if key == "company":
                continue
            if isinstance(value, str) and any(ch.isspace() for ch in value):
                value = json.dumps(value)
            lines.append("  {}: {}".format(key, value))
    return "\n".join(lines) + "\n"


def _review_entry(head, *, valid=True):
    artifacts = {
        "variants/example-role/resume.tex": "a" * 64,
        "variants/example-role/resume.pdf": "b" * 64,
    }
    provenance = {
        "schema": 1, "review_head_sha": head,
        "master_commit": "c" * 40, "master_resume_blob": "d" * 40,
        "master_resume_path": "master/resume.tex",
        "current_jd_sha256": "e" * 64, "profile_sha256": "f" * 64,
        "artifacts": artifacts,
    }
    if not valid:
        provenance["review_head_sha"] = "0" * 40
    audit = {
        "truthfulness": "pass", "eligibility": "eligible",
        "layout_checked": True, "onepage_checked": True, "no_tracker_edit": True,
        "findings": [], "summary": CANARY,
        "posting_url": "https://example.invalid/job/1",
        "current_jd_sha256": provenance["current_jd_sha256"],
        "profile_sha256": provenance["profile_sha256"],
        "master_commit": provenance["master_commit"],
        "master_resume_blob": provenance["master_resume_blob"],
        "artifact_sha256": artifacts,
        "provenance_sha256": review_contract.canonical_sha256(provenance),
        "checked_at": "2026-11-16T05:50:00Z",
    }
    return {"verdict": "approve", "review_head_sha": head,
            "reviewer_version": CANARY, "provenance": provenance, "audit": audit}


def _preflight_record(head, *, accepted=False, code=None):
    hashes = {key: str(index) * 64 for index, key in enumerate((
        "master_sha256", "profile_sha256", "jd_sha256", "role_title_sha256",
        "combined_text_sha256"), 1)}
    report = {
        "schema_version": 1,
        "status": "exact_prior_acceptance" if accepted else "review_required",
        "input_hashes": hashes, "artifact_sha256": "9" * 64, "head": head,
        "reuse_status": "exact_prior_acceptance" if accepted else "review_required",
        "findings": ([] if accepted else [{"category": "content",
                                           "code": code or "unsupported_number",
                                           "message": CANARY}]),
    }
    item = {"report": report}
    if accepted:
        item["owner_record"] = {
            "schema_version": 1, "record_type": "owner_accepted_preflight",
            "accepted": True, "accepted_by": OWNER_NAME,
            "accepted_at": "2026-11-16T05:55:00Z", "head": head,
            "input_hashes": hashes, "artifact_sha256": "9" * 64,
        }
    return item


def _fixture(root, *, tracker=None, queue=None, events=None, reviews=None,
             preflight=None, tracker_text=None, event_text=None,
             review_text=None, preflight_text=None):
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "repo"
    runtime = root / "runtime"
    repo.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)
    tracker_path = repo / "applications.yaml"
    queue_path = runtime / "queue.yaml"
    event_path = runtime / "events.json"
    ingestion_path = runtime / "ingestion.json"
    snapshot_root = runtime / "snapshots"
    review_path = runtime / "reviews.json"
    preflight_path = runtime / "preflight.json"
    if tracker_text is not None:
        tracker_path.write_text(tracker_text, encoding="utf-8")
    else:
        tracker_path.write_text(_yaml(tracker or []), encoding="utf-8")
    queue_path.write_text(_yaml(queue or []), encoding="utf-8")
    default_events = {"schema": 1, "coverage": {
        "complete": True, "observed_at": "2026-11-16T06:00:00Z"}, "events": []}
    event_path.write_text(event_text if event_text is not None else json.dumps(events or default_events),
                          encoding="utf-8")
    # A secret scope key and nested fields are deliberately dropped at ingress.
    ingestion_path.write_text(json.dumps({"schema": 1, "scopes": {
        CANARY: {"last_scan_at": "2026-11-16T05:30:00Z", "mailbox": CANARY}}}),
                              encoding="utf-8")
    snapshot_root.mkdir(exist_ok=True)
    review_doc = reviews if reviews is not None else {"reviewed": {}}
    review_path.write_text(review_text if review_text is not None else json.dumps(review_doc),
                           encoding="utf-8")
    preflight_doc = preflight if preflight is not None else {"schema": 1, "records": []}
    preflight_path.write_text(preflight_text if preflight_text is not None else json.dumps(preflight_doc),
                              encoding="utf-8")
    return {"repo": str(repo), "tracker": str(tracker_path), "queue": str(queue_path),
            "ledger": str(event_path), "ingestion": str(ingestion_path),
            "snapshot_root": str(snapshot_root), "review_state": str(review_path),
            "preflight_state": str(preflight_path)}


def _collect(db, paths, *, now=NOW, batch_size=100, interrupt_after_batches=None):
    return projection.collect_projection(db_path=str(db), **paths,
                                         as_of="2026-11-16", now=now,
                                         batch_size=batch_size,
                                         interrupt_after_batches=interrupt_after_batches)


def _db_bytes(db):
    return {path.name: path.read_bytes() for path in db.parent.glob(db.name + "*")
            if path.is_file()}


with tempfile.TemporaryDirectory(prefix="synthetic-projection-i11-") as temp:
    root = Path(temp)
    paths = _fixture(root,
        tracker=[{"company": "Example Co", "role": "Software Intern",
                  "status": "Applied", "applied_date": "2026-11-16",
                  "notes": CANARY, "raw_nested": {"private": CANARY}}],
        queue=[{"company": "Example Co", "role": "Software Intern",
                "status": "queued", "pr_number": 41,
                "notes": CANARY}],
        events={"schema": 1, "coverage": {"complete": True,
                   "observed_at": "2026-11-16T06:00:00Z"},
                "events": [{"event_id": CANARY, "event_type": "offer",
                            "application_id": "pr:41", "occurred_at": "2026-11-16T05:00:00Z",
                            "verification": "verified", "origin": "unknown",
                            "evidence": CANARY, "raw_excerpt": CANARY}]},
        reviews={"reviewed": {"41": _review_entry(HEAD1),
                               "42": _review_entry(HEAD2, valid=False)}},
        preflight={"schema": 1, "records": [
            _preflight_record(HEAD1, accepted=True),
            _preflight_record(HEAD2, accepted=False, code="unsupported_number")]})
    db = root / "runtime" / "projection" / "index.sqlite3"
    future_mtime = datetime(2040, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(paths["tracker"], (future_mtime, future_mtime))
    first = _collect(db, paths)
    assert first["published"] is True, first
    snapshot = projection.read_snapshot(str(db))
    assert snapshot["schema_version"] == 1
    assert snapshot["sources"]["tracker"]["status"] == "stale"
    assert snapshot["sources"]["tracker"]["error_code"] == "observation_time_unknown"
    assert snapshot["sources"]["events"]["status"] == "fresh"
    assert snapshot["facts"]["events"][0]["event_type"] == "offer"
    review_quality = snapshot["facts"]["review_quality"]
    assert review_quality["unique_artifact_heads"] == 2
    by_head = {row["artifact_head"]: row for row in review_quality["records"]}
    assert by_head[HEAD1]["model_acceptance"] == "accepted"
    assert by_head[HEAD1]["owner_acceptance"] == "accepted"
    assert by_head[HEAD2]["model_acceptance"] == "unknown"
    assert by_head[HEAD2]["owner_acceptance"] == "unknown"
    assert review_quality["rework_by_code"] == {"unsupported_number": 1}
    exported = json.dumps(snapshot, sort_keys=True)
    assert CANARY not in exported and OWNER_NAME not in exported
    stored = b"".join(_db_bytes(db).values())
    assert CANARY.encode() not in stored and OWNER_NAME.encode() not in stored

    # Equivalent content has one stable ID across replay, later collection time,
    # and an independent cold rebuild. Collection timestamps remain separate.
    replay = _collect(db, paths, now=datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc))
    assert replay["snapshot_id"] == first["snapshot_id"]
    replayed = projection.read_snapshot(str(db))
    assert replayed["generated_at"] != snapshot["generated_at"]
    rebuilt_db = root / "runtime" / "rebuild" / "index.sqlite3"
    rebuilt = _collect(rebuilt_db, paths,
                       now=datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc))
    assert rebuilt["snapshot_id"] == first["snapshot_id"]
    assert projection.format_compact(replayed).startswith(
        "Jobs snapshot {} · as of 2026-11-16".format(first["snapshot_id"]))

    # Report entry points use only the selected cached snapshot and formatter.
    before = _db_bytes(db)
    real_connect = sqlite3.connect
    def readonly_connect(database, *args, **kwargs):
        assert kwargs.get("uri") is True and "mode=ro" in database
        return real_connect(database, *args, **kwargs)
    with mock.patch.object(projection.sqlite3, "connect", side_effect=readonly_connect), \
            mock.patch.object(projection, "_collect_inputs",
                              side_effect=AssertionError("cached read attempted collection")), \
            mock.patch.object(socket, "create_connection",
                              side_effect=AssertionError("cached read attempted network")), \
            mock.patch.object(urllib.request, "urlopen",
                              side_effect=AssertionError("cached read attempted network")):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert projection.main(["report", "compact", "--db", str(db)]) == 0
        assert out.getvalue().strip() == projection.format_compact(replayed)
        health_out = io.StringIO()
        with contextlib.redirect_stdout(health_out):
            assert pipeline_health.main(["--db", str(db), "--json"]) == 0
        health_report = json.loads(health_out.getvalue())
        assert health_report["snapshot_id"] == first["snapshot_id"]
        funnel_out = io.StringIO()
        with contextlib.redirect_stdout(funnel_out):
            assert funnel_report.main(["--db", str(db), "--json"]) == 0
        funnel = json.loads(funnel_out.getvalue())
        assert funnel["snapshot_id"] == first["snapshot_id"]
    assert _db_bytes(db) == before

    # A new multi-batch replacement cannot leak its first batch. Its durable
    # cursor resumes after restart and deletion is published as an empty set.
    old_id = replayed["snapshot_id"]
    Path(paths["queue"]).write_text("[]\n", encoding="utf-8")
    Path(paths["tracker"]).write_text(_yaml([
        {"company": "Example Co", "role": "First Role", "status": "Applied"},
        {"company": "Example Co", "role": "Second Role", "status": "Applied"},
    ]), encoding="utf-8")
    partial = _collect(db, paths, batch_size=1, interrupt_after_batches=1)
    assert partial["published"] is False
    during = projection.read_snapshot(str(db))
    assert during["snapshot_id"] == old_id
    assert during["collection"]["status"] == "in_progress"
    check = sqlite3.connect(str(db))
    try:
        cursor, total = check.execute(
            "SELECT cursor,total FROM source_generations WHERE source='tracker' AND state='staging'").fetchone()
        assert cursor == 1 and total == 2
    finally:
        check.close()
    resumed = _collect(db, paths, batch_size=1)
    assert resumed["published"] is True
    assert len(projection.read_snapshot(str(db))["facts"]["applications"]) == 2
    Path(paths["tracker"]).write_text("[]\n", encoding="utf-8")
    deleted = _collect(db, paths)
    assert deleted["published"] is True
    deleted_snapshot = projection.read_snapshot(str(db))
    assert deleted_snapshot["facts"]["applications"] == []
    assert deleted_snapshot["sources"]["tracker"]["empty"] is True

    # Per-source failure keeps last-good event facts while other successful
    # replacement sources update; recovery clears only its own error.
    events_path = Path(paths["ledger"])
    events_path.write_text("{malformed", encoding="utf-8")
    Path(paths["tracker"]).write_text(_yaml([
        {"company": "Example Co", "role": "Recovered Tracker", "status": "Applied"}]),
        encoding="utf-8")
    Path(paths["queue"]).write_text(_yaml([
        {"company": "Queue Co", "role": "Queued Role", "status": "queued", "pr_number": 52}]),
        encoding="utf-8")
    failed = _collect(db, paths)
    failed_snapshot = projection.read_snapshot(str(db))
    assert failed_snapshot["sources"]["events"]["status"] == "stale"
    assert failed_snapshot["sources"]["events"]["error_code"] == "source_invalid"
    assert failed_snapshot["sources"]["events"]["error_quarantined_count"] == 1
    assert failed_snapshot["facts"]["events"][0]["event_type"] == "offer"
    assert failed_snapshot["facts"]["applications"][0]["row"]["role"] == "Recovered Tracker"
    assert failed_snapshot["sources"]["queue"]["record_count"] == 1
    events_path.write_text(json.dumps({"schema": 1, "coverage": {
        "complete": True, "observed_at": "2026-11-16T06:00:00Z"},
        "events": [{"event_id": CANARY, "event_type": "interview_invitation",
                    "application_id": "pr:41", "occurred_at": "2026-11-16T05:00:00Z",
                    "verification": "verified", "origin": "unknown"}]}),
        encoding="utf-8")
    recovered = _collect(db, paths, now=datetime(2026, 11, 16, 6, 40, tzinfo=timezone.utc))
    recovered_snapshot = projection.read_snapshot(str(db))
    assert recovered_snapshot["sources"]["events"]["status"] == "fresh"
    assert recovered_snapshot["sources"]["events"]["error_code"] is None
    assert recovered_snapshot["sources"]["tracker"]["error_code"] == "observation_time_unknown"
    assert recovered_snapshot["facts"]["events"][0]["event_type"] == "interview_invitation"

    # Cold failures stay unavailable; a valid empty source is collected as
    # empty/stale, never confused with a cold error.
    cold_root = root / "cold"
    cold_paths = _fixture(cold_root)
    os.remove(cold_paths["tracker"])
    os.remove(cold_paths["ledger"])
    cold_db = cold_root / "runtime" / "projection" / "index.sqlite3"
    cold = _collect(cold_db, cold_paths)
    cold_snapshot = projection.read_snapshot(str(cold_db))
    assert cold_snapshot["sources"]["tracker"]["status"] == "unavailable"
    assert cold_snapshot["sources"]["tracker"]["error_code"] == "source_missing"
    assert cold_snapshot["sources"]["queue"]["status"] == "stale"
    assert cold_snapshot["sources"]["queue"]["record_count"] == 0
    assert cold_snapshot["sources"]["queue"]["empty"] is True
    assert cold_snapshot["sources"]["events"]["status"] == "unavailable"

print("test_projection: incremental generations, privacy, cached reads and review facts passed")
