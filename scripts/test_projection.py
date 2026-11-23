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
import intake_reporting
import pipeline_health
import projection
import review_contract


NOW = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


# Collection and read-time freshness must use the same synthetic clock.
_projection_clock = mock.patch.object(projection, "datetime", FixtureClock)
_projection_clock.start()
CANARY = "PRIVATE-CANARY-I11-DO-NOT-PERSIST"
OWNER_NAME = "PRIVATE-OWNER-NAME-I11"
HEAD1 = "1" * 40
HEAD2 = "2" * 40


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)


# Read-time freshness checks must use the fixture clock, not the machine date.
projection.datetime = FixtureDateTime


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


def _review_entry(head, *, legacy=False):
    artifacts = {
        "variants/example-role/resume.tex": "a" * 64,
        "variants/example-role/resume.pdf": "b" * 64,
    }
    posting_evidence = review_contract.posting_evidence(
        {"link": "https://example.invalid/job/1"},
        "Synthetic description for a software internship.")
    provenance = {
        "schema": 1, "review_head_sha": head,
        "master_commit": "c" * 40, "master_resume_blob": "d" * 40,
        "master_resume_path": "master/resume.tex",
        "current_jd_sha256": posting_evidence["current_jd_sha256"],
        "profile_sha256": "f" * 64,
        "artifacts": artifacts,
    }
    if not legacy:
        provenance.update(posting_evidence)
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
    intake_dir = runtime / "intake"
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
    intake_dir.mkdir(exist_ok=True)
    review_doc = reviews if reviews is not None else {"reviewed": {}}
    review_path.write_text(review_text if review_text is not None else json.dumps(review_doc),
                           encoding="utf-8")
    preflight_doc = preflight if preflight is not None else {"schema": 1, "records": []}
    preflight_path.write_text(preflight_text if preflight_text is not None else json.dumps(preflight_doc),
                              encoding="utf-8")
    return {"repo": str(repo), "tracker": str(tracker_path), "queue": str(queue_path),
            "ledger": str(event_path), "ingestion": str(ingestion_path),
            "intake_dir": str(intake_dir),
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
                               "42": _review_entry(HEAD2, legacy=True)}},
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
    assert snapshot["sources"]["events"]["status"] == "fresh", snapshot["sources"]["events"]
    assert snapshot["facts"]["events"][0]["event_type"] == "offer"
    review_quality = snapshot["facts"]["review_quality"]
    assert review_quality["unique_artifact_heads"] == 2
    by_head = {row["artifact_head"]: row for row in review_quality["records"]}
    assert by_head[HEAD1]["model_acceptance"] == "accepted"
    assert by_head[HEAD1]["owner_acceptance"] == "accepted"
    assert by_head[HEAD2]["model_acceptance"] == "accepted"
    assert by_head[HEAD2]["provenance_status"] == "unknown"
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
    compact = projection.format_compact(replayed)
    assert "Daily goal (Asia/Singapore): 1 / 1 confirmed" in compact, compact
    assert "Queue: queued 1" in compact, compact
    assert "Attention: stale in progress 0" in compact, compact
    assert "Sources: " in compact and "tracker=stale" in compact, compact
    cold_unavailable = {
        "schema_version": 1, "snapshot_id": "c" * 64,
        "generated_at": "2026-11-16T06:00:00Z", "as_of": "2026-11-16",
        "sources": {"tracker": {"status": "unavailable"},
                    "queue": {"status": "unavailable"}},
        "facts": {"applications": [], "attempts": []},
    }
    unavailable_compact = projection.format_compact(cold_unavailable)
    assert "Daily goal (Asia/Singapore): unavailable / 1 confirmed" in unavailable_compact
    assert "Queue: unavailable" in unavailable_compact

    # Report entry points use only the selected cached snapshot and formatter.
    before = _db_bytes(db)
    real_connect = sqlite3.connect
    def readonly_connect(database, *args, **kwargs):
        assert kwargs.get("uri") is True and "mode=ro" in database
        return real_connect(database, *args, **kwargs)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)
    with mock.patch.object(projection.sqlite3, "connect", side_effect=readonly_connect), \
            mock.patch.object(projection, "datetime", FixedDatetime), \
            mock.patch.object(projection, "_collect_inputs",
                              side_effect=AssertionError("cached read attempted collection")), \
            mock.patch.object(socket, "create_connection",
                              side_effect=AssertionError("cached read attempted network")), \
            mock.patch.object(urllib.request, "urlopen",
                              side_effect=AssertionError("cached read attempted network")):
        replayed = projection.read_snapshot(str(db))
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
    before_event_failure = projection.read_snapshot(str(db))
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
    assert failed_snapshot["sources"]["events"]["observed_at"] == before_event_failure["sources"]["events"]["observed_at"]
    assert failed_snapshot["sources"]["events"]["last_success_at"] == before_event_failure["sources"]["events"]["last_success_at"]
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

    # Manifest collection stores hash-only provenance. An I/O failure retains
    # the complete prior generation; a confirmed deletion clears it.
    manifest_paths = _fixture(root / "manifest-case", tracker=[{
        "company": "Manifest Co", "role": "Intern", "pr_number": 41,
        "status": "Applied", "applied_date": "2026-11-16"}])
    manifest = {"schema_version": 1, "captured_at": "2026-11-16T05:00:00Z",
                "canonical_application_id": "pr:41", "attempt_id": "attempt-41",
                "confirmation": {"submitted_at": "2026-11-16", "evidence": CANARY},
                "artifacts": {"resume_pdf": {"sha256": "a" * 64, "path": CANARY}}}
    encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    manifest_id = hashlib.sha256(encoded.encode()).hexdigest()
    manifest_directory = Path(manifest_paths["snapshot_root"]) / "manifests"
    manifest_directory.mkdir()
    manifest_path = manifest_directory / (manifest_id + ".json")
    manifest_path.write_text(encoded, encoding="utf-8")
    Path(manifest_paths["queue"]).write_text(_yaml([{
        "company": "Manifest Co", "role": "Intern", "pr_number": 41,
        "status": "submitted", "submitted_at": "2026-11-16T05:00:00Z",
        "snapshot_provenance": "complete", "snapshot_id": manifest_id,
        "attempt_id": "attempt-41"}]), encoding="utf-8")
    manifest_db = root / "manifest-db" / "index.sqlite3"
    _collect(manifest_db, manifest_paths)
    assert projection.read_snapshot(str(manifest_db))["facts"]["applications"][0]["resume_hash"] == "a" * 64
    original_json_read = projection._read_json
    def unreadable_manifest(path):
        if Path(path) == manifest_path:
            raise PermissionError("synthetic manifest I/O failure")
        return original_json_read(path)
    with mock.patch.object(projection, "_read_json", side_effect=unreadable_manifest):
        _collect(manifest_db, manifest_paths)
    retained = projection.read_snapshot(str(manifest_db))
    assert retained["facts"]["applications"][0]["resume_hash"] == "a" * 64
    assert retained["sources"]["snapshots"]["status"] == "stale"
    assert retained["sources"]["snapshots"]["error_code"] == "source_unreadable"
    assert CANARY not in json.dumps(retained)
    assert CANARY.encode() not in b"".join(_db_bytes(manifest_db).values())
    manifest_path.unlink()
    _collect(manifest_db, manifest_paths)
    deleted_manifest = projection.read_snapshot(str(manifest_db))
    assert deleted_manifest["facts"]["applications"][0]["resume_hash"] is None
    assert deleted_manifest["sources"]["snapshots"]["empty"] is True

    # Arbitrary query values never enter storage. Job IDs remain distinct.
    assert projection._safe_url("https://jobs.example/job?job_id=41#" + CANARY) == "https://jobs.example/job?job_id=41"
    assert projection._safe_url("https://jobs.example/job?job_id=41") != projection._safe_url("https://jobs.example/job?job_id=42")
    url_paths = _fixture(root / "url-case", tracker=[{
        "company": "URL Co", "role": "Intern", "link": "https://jobs.example/job?ref=" + CANARY}])
    url_db = root / "url-db" / "index.sqlite3"
    _collect(url_db, url_paths)
    sanitized = projection.read_snapshot(str(url_db))
    assert sanitized["sources"]["tracker"]["coverage_status"] == "partial"
    assert sanitized["sources"]["tracker"]["quarantined_count"] == 1
    assert sanitized["sources"]["tracker"]["error_code"] == "partial_coverage"
    assert CANARY not in json.dumps(sanitized)
    assert CANARY.encode() not in b"".join(_db_bytes(url_db).values())

    # Explicit malformed PR references cannot fall back to a company/role join.
    invalid_prs = [-1, 0, "0", "001", "https://github.com/example/resume/issues/42"]
    invalid_paths = _fixture(root / "invalid-prs", tracker=[
        {"company": "PR Co", "role": "Intern", "pr": value, "status": "Interview"}
        for value in invalid_prs], queue=[
            {"company": "PR Co", "role": "Intern", "pr_number": 42, "status": "queued"}])
    invalid_db = root / "invalid-pr-db" / "index.sqlite3"
    _collect(invalid_db, invalid_paths)
    invalid_snapshot = projection.read_snapshot(str(invalid_db))
    assert invalid_snapshot["sources"]["tracker"]["quarantined_count"] == len(invalid_prs)
    assert len(invalid_snapshot["facts"]["applications"]) == 1
    assert invalid_snapshot["facts"]["applications"][0]["row"].get("status") != "Interview"

    # Credential labels in identity fields are quarantined; optional text is
    # redacted. Neither exported facts nor SQLite bytes retain their values.
    labels = ["access_token", "api_key", "refreshToken", "client-secret"]
    credential_paths = _fixture(root / "credentials", tracker=[
        {"company": label + "=" + CANARY, "role": "Intern", "pr_number": 70 + i}
        for i, label in enumerate(labels)] + [
            {"company": "Safe Peer", "role": "Intern", "pr_number": 80,
             "source": '"access_token":' + CANARY}])
    credential_db = root / "credential-db" / "index.sqlite3"
    _collect(credential_db, credential_paths)
    credential_snapshot = projection.read_snapshot(str(credential_db))
    assert credential_snapshot["sources"]["tracker"]["quarantined_count"] == len(labels)
    assert credential_snapshot["sources"]["tracker"]["redacted_count"] == 1
    assert len(credential_snapshot["facts"]["applications"]) == 1
    assert CANARY not in json.dumps(credential_snapshot)
    assert CANARY.encode() not in b"".join(_db_bytes(credential_db).values())

    # Legacy tracker PR/status forms, schema-1 evidence without coverage, and
    # the legacy folders map retain safe facts while exposing partial coverage.
    compat_paths = _fixture(root / "compatibility", tracker_text=_yaml([
        {"company": "Legacy Co", "role": "Platform Intern",
         "pr": "https://github.com/example/resume/pull/41",
         "status": "interviewing", "applied_date": "not-a-date"},
        {"company": "Unsafe Co", "role": "Unsafe Role",
         "link": "https://jobs.example/role?private=" + CANARY,
         "status": "Applied"},
        {"company": "Ambiguous Co", "role": "Ambiguous Role",
         "pr": 98, "pr_number": 99, "status": "Applied"},
        {"company": "Current Co", "role": "Current Intern",
         "pr_number": 42, "stage": "Applied", "source": "token=" + CANARY},
    ]))
    legacy_event = {"event_id": "legacy-event-1", "event_type": "interview_invitation",
                    "application_id": "pr:41", "occurred_at": "bad-time",
                    "verification": "unrecognized", "origin": "candidate@example.invalid"}
    Path(compat_paths["ledger"]).write_text(
        json.dumps({"schema": 1, "events": [legacy_event,
                  {"event_id": "legacy-event-2", "event_type": "offer",
                   "application_id": "pr:42", "occurred_at": "2026-11-16T05:00:00Z",
                   "verification": "verified", "origin": "unknown"}]}),
        encoding="utf-8")
    Path(compat_paths["ingestion"]).write_text(json.dumps({"schema": 1, "folders": {
        "INBOX": {"last_scan_at": "2026-11-16T05:30:00Z", "account": CANARY},
        "ARCHIVE": {"last_scan_at": "not-a-time"},
        "BROKEN": "not-a-folder-record"}}), encoding="utf-8")
    compat_db = root / "compatibility-db" / "index.sqlite3"
    _collect(compat_db, compat_paths)
    compat_snapshot = projection.read_snapshot(str(compat_db))
    assert {row["row"].get("role") for row in compat_snapshot["facts"]["applications"]} == {
        "Platform Intern", "Current Intern"}
    assert compat_snapshot["sources"]["tracker"]["coverage_status"] == "partial"
    assert compat_snapshot["sources"]["tracker"]["quarantined_count"] == 2
    assert compat_snapshot["sources"]["tracker"]["redacted_count"] == 2
    assert compat_snapshot["sources"]["tracker"]["coverage_reasons"] == [
        "optional_metadata_redacted", "records_quarantined"]
    assert compat_snapshot["sources"]["events"]["coverage_status"] == "partial"
    assert "legacy_coverage_missing" in compat_snapshot["sources"]["events"]["coverage_reasons"]
    assert compat_snapshot["facts"]["event_coverage"]["complete"] is False
    compat_events = compat_snapshot["facts"]["events"]
    assert {item["event_type"] for item in compat_events} == {
        "interview_invitation", "offer"}
    redacted_event = next(item for item in compat_events
                          if item["event_type"] == "interview_invitation")
    assert "occurred_at" not in redacted_event
    assert redacted_event["verification"] == "unknown"
    assert redacted_event["origin"] == "unknown"
    assert compat_snapshot["sources"]["ingestion"]["coverage_status"] == "partial"
    assert compat_snapshot["sources"]["ingestion"]["quarantined_count"] == 1
    assert compat_snapshot["sources"]["ingestion"]["redacted_count"] == 1
    assert compat_snapshot["sources"]["ingestion"]["coverage_reasons"] == [
        "optional_metadata_redacted", "records_quarantined"]
    assert compat_snapshot["facts"]["ingestion"]["scope_count"] == 2
    assert compat_snapshot["facts"]["ingestion"]["last_scan_at"] == "2026-11-16T05:30:00Z"
    assert CANARY not in json.dumps(compat_snapshot)
    assert CANARY.encode() not in b"".join(_db_bytes(compat_db).values())
    legacy_form_path = root / "legacy-tracker.yaml"
    current_form_path = root / "current-tracker.yaml"
    legacy_form_path.write_text(_yaml([{
        "company": "Same Co", "role": "Platform Intern",
        "pr": "https://github.com/example/resume/pull/43",
        "status": "interviewing"}]), encoding="utf-8")
    current_form_path.write_text(_yaml([{
        "company": "Same Co", "role": "Platform Intern",
        "pr_number": 43, "stage": "Interview"}]), encoding="utf-8")
    legacy_form = projection._read_tracker(str(legacy_form_path))["records"]
    current_form = projection._read_tracker(str(current_form_path))["records"]
    import application_identity
    import summary
    assert application_identity.application_id(legacy_form[0]) == \
        application_identity.application_id(current_form[0]) == "pr:43"
    assert summary.row_stage(legacy_form[0]) == summary.row_stage(current_form[0]) == "Interview"

    # Existing schema-1 caches gain the new aggregate coverage columns without
    # replacing their last-good source timestamps.
    migration_db = root / "migration.sqlite3"
    old_db = sqlite3.connect(str(migration_db))
    old_db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key,value) VALUES('schema_version','1');
        CREATE TABLE source_state (
            source TEXT PRIMARY KEY, active_generation TEXT, status TEXT NOT NULL,
            observed_at TEXT, collected_at TEXT, last_success_at TEXT,
            freshness_seconds INTEGER NOT NULL, error_code TEXT,
            record_count INTEGER NOT NULL DEFAULT 0,
            quarantined_count INTEGER NOT NULL DEFAULT 0,
            error_quarantined_count INTEGER NOT NULL DEFAULT 0,
            empty INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO source_state(source,status,observed_at,last_success_at,freshness_seconds)
        VALUES('events','stale','2026-11-15T05:00:00Z','2026-11-16T06:00:00Z',604800);
    """)
    old_db.close()
    migrated = projection._connect_writer(str(migration_db))
    try:
        migrated_columns = {row[1] for row in migrated.execute("PRAGMA table_info(source_state)")}
        assert {"coverage_status", "coverage_reasons", "redacted_count"}.issubset(migrated_columns)
        preserved = migrated.execute(
            "SELECT observed_at,last_success_at FROM source_state WHERE source='events'").fetchone()
        assert tuple(preserved) == ("2026-11-15T05:00:00Z", "2026-11-16T06:00:00Z")
    finally:
        migrated.close()

    # Publish between the reader's payload and progress queries. The reader
    # must return both from its original SQLite transaction.
    interleave_paths = _fixture(root / "interleave", tracker=[{
        "company": "Atomic Co", "role": "Old Intern", "pr_number": 71}])
    interleave_db = root / "interleave-db" / "index.sqlite3"
    _collect(interleave_db, interleave_paths)
    original_snapshot = projection.read_snapshot(str(interleave_db))
    writer = projection._connect_writer(str(interleave_db))
    projection._put_progress(writer, {"status": "in_progress", "started_at": NOW.isoformat(), "sources": {}})
    writer.close()
    Path(interleave_paths["tracker"]).write_text(_yaml([{
        "company": "Atomic Co", "role": "New Intern", "pr_number": 71}]), encoding="utf-8")
    loads = json.loads
    fired = [False]
    def publish_during_read(text, *args, **kwargs):
        value = loads(text, *args, **kwargs)
        if (not fired[0] and isinstance(value, dict)
                and value.get("snapshot_id") == original_snapshot["snapshot_id"]):
            fired[0] = True
            _collect(interleave_db, interleave_paths)
        return value
    with mock.patch.object(projection.json, "loads", side_effect=publish_during_read):
        coherent = projection.read_snapshot(str(interleave_db))
    assert fired[0]
    assert coherent["snapshot_id"] == original_snapshot["snapshot_id"]
    assert coherent["collection"]["status"] == "in_progress"
    assert projection.read_snapshot(str(interleave_db))["snapshot_id"] != coherent["snapshot_id"]

    # Catalog facts belong to the published generation. Reports neither open
    # the external catalog nor observe its edits before the next collection.
    import company_catalog
    catalog_paths = _fixture(root / "catalog-source", tracker=[{
        "company": "Catalog Co", "role": "Intern", "pr_number": 81,
        "status": "Applied", "applied_date": "2026-11-15"}])
    catalog_db = root / "catalog-db" / "index.sqlite3"
    company = company_catalog._record({
        "key": "catalog-co", "aliases": ["Catalog Co"], "size": "SME",
        "footprint": "multinational", "primary_industry": "software"})
    with mock.patch.object(company_catalog, "load", return_value={
            "schema_version": 1, "records": [company]}) as loader:
        _collect(catalog_db, catalog_paths)
        assert loader.call_count == 1
    catalog_snapshot = projection.read_snapshot(str(catalog_db))
    assert catalog_snapshot["facts"]["company_catalog"]["records"][0]["size"] == "SME"
    output = io.StringIO()
    with mock.patch.object(company_catalog, "load", side_effect=AssertionError("uncached read")):
        with contextlib.redirect_stdout(output):
            assert projection.main(["report", "analytics", "--db", str(catalog_db),
                                    "--period", "alltime"]) == 0
    assert json.loads(output.getvalue())["companies"]["size"]["SME"] == 1
    with mock.patch.object(company_catalog, "load", side_effect=company_catalog.CatalogError("invalid")):
        _collect(catalog_db, catalog_paths)
    retained_catalog = projection.read_snapshot(str(catalog_db))
    assert retained_catalog["facts"]["company_catalog"] == catalog_snapshot["facts"]["company_catalog"]
    assert retained_catalog["sources"]["companies"]["error_code"] == "source_invalid"

    # The health view joins latest discovery/enqueue runs with queue ages,
    # browser ownership, and snapshot/sync recovery backlog.
    diagnostics_paths = _fixture(root / "intake-health", queue=[
        {"company": "Ready Co", "role": "Ready Intern", "pr_number": 101,
         "status": "queued", "enqueued_at": "2026-11-15T06:00:00Z",
         "approved_at": "2026-11-15T06:00:00Z", "session_ref": "private@example.invalid"},
        {"company": "Owner Co", "role": "Owner Intern", "pr_number": 102,
         "status": "in_progress", "attempt_id": "attempt-102",
         "claimed_at": "2026-11-16T01:00:00Z", "runner_id": "runner-a",
         "session_ref": "session:owner-102"},
        {"company": "Released Co", "role": "Released Intern", "pr_number": 106,
         "status": "in_progress", "attempt_id": "attempt-106",
         "claimed_at": "2026-11-15T06:00:00Z", "runner_id": "runner-b",
         "session_ref": "session:owner-106",
         "browser_owner_released_at": "2026-11-16T02:00:00Z"},
        {"company": "Blocked Co", "role": "Blocked Intern", "pr_number": 103,
         "status": "blocked", "blocker_code": "eligibility_hold",
         "blocked_at": "2026-11-13T06:00:00Z"},
        {"company": "Unknown Co", "role": "Unknown Intern", "pr_number": 104,
         "status": "outcome_unknown", "attempt_state": "outcome_unknown",
         "claimed_at": "2026-11-15T06:00:00Z",
         "outcome_unknown_at": "2026-11-16T00:00:00Z"},
        {"company": "Submitted Co", "role": "Submitted Intern", "pr_number": 105,
         "status": "submitted", "attempt_id": "attempt-105",
         "submitted_at": "2026-11-15T06:00:00Z",
         "tracker_sync_pending_at": "2026-11-14T06:00:00Z",
         "tracker_sync": "pending", "snapshot_provenance": "complete",
         "snapshot_id": "a" * 64},
    ])
    intake_dir = Path(diagnostics_paths["intake_dir"])
    source_id = intake_reporting.source_id("company", "synthetic")
    discovery_run = {"recorded_at": "2026-11-16T04:00:00Z",
                     **intake_reporting.sanitize_summary({
                         "outcome": "healthy_empty",
                         "counts": {"processed": 0, "accepted": 0, "added": 0},
                         "reasons": {"no_postings": 1},
                         "sources": [{"kind": "company", "source_id": source_id,
                                      "fetch": "ok", "parse": "empty",
                                      "processed": 0, "accepted": 0}],
                     })}
    enqueue_run = {"recorded_at": "2026-11-16T05:00:00Z",
                   **intake_reporting.sanitize_summary({
                       "outcome": "partial_failure",
                       "counts": {"observed": 8, "added": 0,
                                  "excluded": 2, "pending_metadata": 1,
                                  "active_work": 2, "closed_history": 3},
                       "reasons": {"no_eligible_prs": 1},
                   })}
    (intake_dir / "discovery.json").write_text(
        json.dumps({"schema_version": 1, "runs": [discovery_run]}), encoding="utf-8")
    (intake_dir / "enqueue.json").write_text(
        json.dumps({"schema_version": 1, "runs": [enqueue_run]}), encoding="utf-8")
    diagnostics_db = root / "intake-health-db" / "index.sqlite3"
    _collect(diagnostics_db, diagnostics_paths)
    diagnostics_snapshot = projection.read_snapshot(str(diagnostics_db))
    health = pipeline_health.build_report(
        diagnostics_snapshot, timezone_name="Asia/Singapore", weekday_goal=1,
        weekend_goal=0, ready_buffer=1, as_of="2026-11-16")
    assert health["intake"]["discovery"]["status"] == "healthy_empty"
    assert health["intake"]["discovery"]["age_seconds"] == 7200
    assert health["intake"]["enqueue"]["status"] == "partial_failure"
    assert health["intake"]["enqueue"]["latest"]["counts"]["closed_history"] == 3
    assert health["intake"]["enqueue"]["latest"]["counts"]["pending_metadata"] == 1
    assert health["ready_buffer"]["queued"] == 1
    assert health["ready_buffer"]["oldest_age_seconds"] == 86400
    assert health["attempts"]["blocked"] == 1
    assert health["attempts"]["oldest_blocked_age_seconds"] == 3 * 86400
    assert health["attempts"]["oldest_unknown_age_seconds"] == 6 * 3600
    assert health["browser_ownership"]["active"] == 1
    assert health["browser_ownership"]["released"] == 1
    assert health["browser_ownership"]["owners"] == [{
        "runner_id": "runner-a", "session_ref": "session:owner-102",
        "attempt_age_seconds": 5 * 3600}]
    assert health["snapshot_gaps"]["known"] == 1
    assert health["snapshot_gaps"]["oldest_age_seconds"] == 86400
    assert health["tracker_sync"]["pending_or_error"] == 1
    assert health["tracker_sync"]["oldest_age_seconds"] == 2 * 86400
    assert "pending metadata 1" in pipeline_health.render_text(health)
    exported_health = json.dumps(health)
    assert "private@example.invalid" not in exported_health
    assert "PRIVATE-CANARY" not in exported_health

    # No history is unknown, while one malformed lane leaves the good lane
    # usable and marks intake coverage partial.
    missing_paths = _fixture(root / "missing-intake")
    missing_db = root / "missing-intake-db" / "index.sqlite3"
    _collect(missing_db, missing_paths)
    missing_snapshot = projection.read_snapshot(str(missing_db))
    missing_health = pipeline_health.build_report(
        missing_snapshot, timezone_name="Asia/Singapore", weekday_goal=1,
        weekend_goal=0, ready_buffer=1, as_of="2026-11-16")
    assert missing_health["intake"]["discovery"]["status"] == "unknown"
    assert missing_health["intake"]["source_status"] == "unavailable"
    partial_paths = _fixture(root / "partial-intake")
    partial_dir = Path(partial_paths["intake_dir"])
    (partial_dir / "discovery.json").write_text(
        json.dumps({"schema_version": 1, "runs": [discovery_run]}), encoding="utf-8")
    (partial_dir / "enqueue.json").write_text("{malformed", encoding="utf-8")
    partial_db = root / "partial-intake-db" / "index.sqlite3"
    _collect(partial_db, partial_paths)
    partial_snapshot = projection.read_snapshot(str(partial_db))
    partial_health = pipeline_health.build_report(
        partial_snapshot, timezone_name="Asia/Singapore", weekday_goal=1,
        weekend_goal=0, ready_buffer=1, as_of="2026-11-16")
    assert partial_snapshot["sources"]["intake"]["coverage_status"] == "partial"
    assert partial_health["intake"]["discovery"]["status"] == "healthy_empty"
    assert partial_health["intake"]["enqueue"]["status"] == "unknown"
    zero_paths = _fixture(root / "zero-admissions")
    zero_dir = Path(zero_paths["intake_dir"])
    zero_run = {"recorded_at": "2026-11-16T05:30:00Z",
                **intake_reporting.sanitize_summary({
                    "outcome": "healthy_empty", "counts": {"admitted": 0}})}
    (zero_dir / "discovery.json").write_text(
        json.dumps({"schema_version": 1, "runs": []}), encoding="utf-8")
    (zero_dir / "enqueue.json").write_text(
        json.dumps({"schema_version": 1, "runs": [zero_run]}), encoding="utf-8")
    zero_db = root / "zero-admissions-db" / "index.sqlite3"
    _collect(zero_db, zero_paths)
    zero_snapshot = projection.read_snapshot(str(zero_db))
    zero_health = pipeline_health.build_report(
        zero_snapshot, timezone_name="Asia/Singapore", weekday_goal=1,
        weekend_goal=0, ready_buffer=1, as_of="2026-11-16")
    assert zero_health["intake"]["enqueue"]["latest"]["reasons"] == {"unknown": 1}
    assert "enqueue [unknown=1]" in pipeline_health.render_text(zero_health)

_projection_clock.stop()
print("test_projection: incremental generations, privacy, cached reads and review facts passed")
