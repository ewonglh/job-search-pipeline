#!/usr/bin/env python3
"""Focused fixed-clock checks for normalized health and funnel reports."""
import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from unittest import mock

import application_identity
import funnel_report
import pipeline_health
import report_snapshot
import summary
import gate_summary


NOW = datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 2, 10)
PDF_HASH = "a" * 64


def _manifest(root, application_id, attempt_id="attempt-a"):
    value = {
        "schema_version": 1,
        "captured_at": "2026-01-01T00:00:00Z",
        "canonical_application_id": application_id,
        "attempt_id": attempt_id,
        "source_commits": {
            "resume_repository_head": {"status": "unknown", "sha": None},
            "variant_source": {"status": "unknown", "sha": None},
            "master_source": {"status": "unknown", "sha": None},
        },
        "operator_material_verification": "Synthetic verified material selection",
        "confirmation": {"submitted_at": "2026-01-01", "evidence": "Synthetic submission receipt evidence"},
        "artifacts": {
            "resume_pdf": {"sha256": PDF_HASH, "bytes": 12, "path": "pdf/" + PDF_HASH + ".pdf"},
            "answers_json": {"sha256": "b" * 64, "bytes": 2, "path": "answers/" + "b" * 64 + ".json"},
        },
    }
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    manifest_id = hashlib.sha256(payload).hexdigest()
    os.makedirs(os.path.join(root, "manifests"), exist_ok=True)
    with open(os.path.join(root, "manifests", manifest_id + ".json"), "wb") as fh:
        fh.write(payload)
    return manifest_id


tracker = [
    {"company": "Atlas Labs", "role": "Data Intern", "source": "Referral",
     "link": "https://jobs.example/a?job_id=1", "status": "Applied",
     "applied_date": "2026-01-01"},
    {"company": "Beacon", "role": "Analyst", "link": "https://jobs.example/b",
     "applied_date": "2026-01-27"},
    {"company": "Cedar", "role": "Research Intern", "source": "Board",
     "link": "https://jobs.example/c", "applied_date": "2026-01-13"},
    {"company": "Conflict", "role": "Engineer", "link": "https://jobs.example/d",
     "applied_date": "2026-01-01"},
    {"company": "Later", "role": "Engineer", "link": "https://jobs.example/e",
     "applied_date": "2026-01-28"},
    {"company": "Undated", "role": "Engineer", "link": "https://jobs.example/f",
     "applied_date": "not-a-date"},
]
queue = [
    {"company": "Atlas Labs", "role": "Data Intern", "source": "Referral",
     "link": "https://jobs.example/a?job_id=1", "status": "submitted",
     "submitted_at": "2026-01-01T12:00:00Z", "tracker_sync": "synced",
     "snapshot_provenance": "complete", "attempt_id": "attempt-a"},
    {"company": "Beacon", "role": "Analyst", "link": "https://jobs.example/b",
     "status": "submitted", "submitted_at": "2026-01-27T12:00:00Z",
     "tracker_sync": "pending", "snapshot_provenance": "incomplete"},
    {"company": "Conflict", "role": "Engineer", "link": "https://jobs.example/d",
     "status": "submitted", "submitted_at": "2026-01-02T12:00:00Z"},
]
aid_a = application_identity.application_id(tracker[0])
aid_b = application_identity.application_id(tracker[1])
aid_c = application_identity.application_id(tracker[2])
aid_d = application_identity.application_id(tracker[3])


def _event(event_id, app, kind, stamp, origin="unknown", **extra):
    result = {"event_id": event_id, "event_type": kind, "application_id": app,
              "occurred_at": stamp, "verification": "verified", "origin": origin}
    result.update(extra)
    return result


events = [
    _event("oa-a", aid_a, "oa_invitation", "2026-01-05T12:00:00Z", "automated"),
    _event("oa-a", aid_a, "oa_invitation", "2026-01-05T12:00:00Z", "automated"),
    _event("interview-a", aid_a, "interview_invitation", "2026-01-08T12:00:00Z"),
    _event("callback-a", aid_a, "recruiter_reply", "2026-01-06T12:00:00Z", "human"),
    _event("unknown-callback", aid_a, "availability_request", "2026-01-07T12:00:00Z"),
    _event("same-day-after", aid_a, "offer", "2026-01-01T13:00:00Z", "human"),
    _event("same-day-before", aid_a, "offer", "2026-01-01T11:00:00Z", "human"),
    _event("quarantined", aid_a, "oa_invitation", "2026-01-05T12:00:00Z", "automated"),
    _event("quarantined", aid_a, "oa_invitation", "2026-01-06T12:00:00Z", "automated"),
    _event("oa-b", aid_b, "oa_passed", "2026-02-10T12:00:00Z", "automated"),
    _event("event-c", aid_c, "interview_completed", "2026-02-01T12:00:00Z", "human"),
    _event("before", aid_a, "offer", "2025-12-31T12:00:00Z", "human"),
    _event("future", aid_a, "offer", "2026-02-11T12:00:00Z", "human"),
    _event("bad-date", aid_a, "offer", "not-a-date", "human"),
    _event("bad-kind", aid_a, "RAW CORRESPONDENCE MUST NOT LEAK", "2026-01-09T12:00:00Z", "human"),
    _event("conflict-event", aid_d, "oa_invitation", "2026-01-05T12:00:00Z", "human"),
]
event_doc = {"schema": 1, "coverage": {"complete": False, "last_scan_at": "2026-02-10T10:00:00Z",
                                       "limitations": ["RAW COVERAGE NOTE MUST NOT LEAK"]},
             "events": events}
sources = {name: {"status": "fresh", "observed_at": "2026-02-10T11:00:00Z",
                  "collected_at": "2026-02-10T12:00:00Z", "last_success_at": "2026-02-10T11:00:00Z",
                  "freshness_seconds": 86400, "error_code": None, "record_count": 1}
           for name in ("tracker", "queue", "events", "ingestion", "snapshots")}


with tempfile.TemporaryDirectory() as root:
    manifest_id = _manifest(root, aid_a)
    queue[0]["snapshot_id"] = manifest_id
    snapshot = report_snapshot.normalize_snapshot(
        tracker, queue, event_doc, {"schema": 1, "scopes": {}}, sources,
        as_of=AS_OF, generated_at=NOW, snapshot_root=root)
    assert snapshot["facts"]["applications"]
    app_a = next(app for app in snapshot["facts"]["applications"]
                 if app["application_id"] == aid_a)
    assert app_a["resume_hash"] == PDF_HASH
    assert "notes" not in json.dumps(snapshot["facts"])
    assert "RAW CORRESPONDENCE MUST NOT LEAK" not in json.dumps(snapshot["facts"])
    assert "RAW COVERAGE NOTE MUST NOT LEAK" not in json.dumps(snapshot["facts"])
    later_sources = {name: dict(value, collected_at="2026-02-11T12:00:00Z", status="stale")
                     for name, value in sources.items()}
    later_snapshot = report_snapshot.normalize_snapshot(
        tracker, queue, event_doc, {"schema": 1, "scopes": {}}, later_sources,
        as_of=AS_OF, generated_at=datetime(2026, 2, 11, 12, tzinfo=timezone.utc),
        snapshot_root=root)
    assert later_snapshot["snapshot_id"] == snapshot["snapshot_id"]

    funnel = funnel_report.build_report(snapshot, timezone_name="Asia/Singapore")
    c14, c28 = funnel["mature_cohorts"]
    assert c14["cutoff_date"] == "2026-01-27" and c14["eligible_denominator"] == 3
    assert c28["cutoff_date"] == "2026-01-13" and c28["eligible_denominator"] == 2
    assert c28["exclusions"]["events_for_ineligible_applications"] >= 1
    assert c28["observed_numerator"]["interview"] == 2
    assert c14["observed_numerator"]["callback"] == 1
    assert c14["observed_numerator"]["oa"] == 2
    assert c14["observed_numerator"]["interview"] == 1
    assert c14["observed_numerator"]["offer"] == 1
    assert c14["exclusions"]["events_outside_window"] >= 1
    assert c14["exclusions"]["unknown_human_origin"] == 1
    assert c14["exclusions"]["events_before_or_on_submission"] >= 1
    assert c14["exclusions"]["events_after_as_of"] >= 1
    assert c14["exclusions"]["invalid_event_date"] >= 1
    assert c14["exclusions"]["unknown_event_type"] == 1
    assert c14["exclusions"]["identity_conflicts"] >= 1
    assert c14["exclusions"]["ambiguous_event_identity"] >= 1
    assert c14["exclusions"]["quarantined_event_ids"] == 1
    assert c14["exclusions"]["invalid_submission_date"] >= 1
    assert c14["exclusions"]["unknown_submission_date"] >= 1
    assert c14["event_type_observed_applications"]["oa_invitation"] == 1
    assert c14["grouped_observed_rates"]["resume_hash"][0]["group"] == PDF_HASH
    assert c14["grouped_observed_rates"]["source"]
    assert c14["response_timing_days"]["callback"]["median_days"] == 5.0
    assert c14["response_timing_days"]["offer"]["median_days"] == 0.0
    assert "not a confirmed non-response" in c14["coverage_caveat"]
    assert json.loads(json.dumps(funnel)) == funnel
    assert funnel_report.render_text(funnel).startswith("Application funnel observations")

    unavailable = dict(snapshot)
    unavailable["sources"] = dict(snapshot["sources"], events={"status": "unavailable",
                                                                   "error_code": "source_missing"})
    no_events = funnel_report.build_report(unavailable)
    assert no_events["mature_cohorts"][0]["observed_numerator"]["oa"] is None
    assert no_events["mature_cohorts"][0]["event_type_observed_applications"] is None
    assert no_events["mature_cohorts"][0]["event_source_status"] == "unavailable"

    unavailable_apps = dict(snapshot)
    unavailable_apps["sources"] = dict(snapshot["sources"], tracker={"status": "unavailable"},
                                       queue={"status": "unavailable"})
    no_app_data = funnel_report.build_report(unavailable_apps)
    assert no_app_data["mature_cohorts"][0]["eligible_denominator"] is None
    assert no_app_data["mature_cohorts"][0]["observed_numerator"]["oa"] is None

    empty = report_snapshot.normalize_snapshot(
        [], [], {"schema": 1, "coverage": {"complete": False,
                    "observed_at": NOW.isoformat()}, "events": []},
        {"schema": 1, "scopes": {}}, sources, as_of=AS_OF, generated_at=NOW)
    empty_report = funnel_report.build_report(empty)
    assert empty_report["mature_cohorts"][0]["observed_numerator"]["oa"] == 0
    assert empty_report["mature_cohorts"][0]["event_source_status"] == "fresh"
    assert empty_report["mature_cohorts"][0]["coverage"]["status"] == "partial"


# Queue-only local-midnight conversion and weekday/weekend goals use one clock.
midnight_snapshot = report_snapshot.normalize_snapshot(
    [], [{"company": "Day Shift", "role": "Intern", "link": "https://jobs.example/day",
         "status": "submitted", "submitted_at": "2026-06-02T16:30:00Z",
         "tracker_sync": "pending", "snapshot_provenance": "incomplete"},
        {"company": "Stale", "role": "Intern", "link": "https://jobs.example/stale",
         "status": "in_progress", "claimed_at": "2026-06-02T00:00:00Z",
         "runner_id": "runner-stale",
         "blocker_code": "portal_timeout", "notes": "SYNTHETIC FREE TEXT MUST NOT LEAK"},
        {"company": "Fresh", "role": "Intern", "link": "https://jobs.example/fresh",
         "status": "in_progress", "claimed_at": "2026-06-02T23:30:00Z",
         "session_ref": "session-fresh"},
        {"company": "Unknown", "role": "Intern", "link": "https://jobs.example/unknown",
         "status": "outcome_unknown"},
        {"company": "Ready", "role": "Intern", "link": "https://jobs.example/ready",
         "status": "queued", "blocker_code": "unstructured note"}],
    {"schema": 1, "coverage": {"complete": False}, "events": []},
    {"schema": 1, "scopes": {}}, sources,
    as_of=date(2026, 6, 3), generated_at=datetime(2026, 6, 3, 0, tzinfo=timezone.utc))
health = pipeline_health.build_report(
    midnight_snapshot, timezone_name="Asia/Singapore", weekday_goal=2,
    weekend_goal=0, ready_buffer=1)
assert health["local_weekday"] == "Wednesday"
assert health["submission_goal"]["target"] == 2
assert health["submission_goal"]["confirmed"] == 1
assert health["submission_goal"]["submissions"]["coverage"] == "complete"
assert health["snapshot_gaps"]["known"] == 1
assert health["tracker_sync"]["pending_or_error"] == 1
assert health["attempts"]["stale_in_progress"] == 1
assert health["attempts"]["in_progress"] == 2
assert health["attempts"]["outcome_unknown"] == 1
assert health["attempts"]["blocked"] == 1
assert health["attempts"]["oldest_blocked_age_seconds"] == 24 * 3600
assert health["attempts"]["unknown_age_unknown_count"] == 1
assert health["ready_buffer"]["target"] == 1
assert health["ready_buffer"]["queued"] == 1
assert health["ready_buffer"]["gap"] == 0
assert health["ready_buffer"]["age_unknown_count"] == 1
assert health["browser_ownership"]["active"] == 0
assert health["browser_ownership"]["unknown"] == 2
assert health["blockers"]["by_code"] == {"portal_timeout": 1}
assert health["blockers"]["unclassified"] == 1
assert "SYNTHETIC FREE TEXT MUST NOT LEAK" not in json.dumps(midnight_snapshot)
assert "runner-stale" not in json.dumps(midnight_snapshot)
assert "session-fresh" not in json.dumps(midnight_snapshot)
assert "Daily pipeline health" in pipeline_health.render_text(health)

weekend = pipeline_health.build_report(
    midnight_snapshot, timezone_name="Asia/Singapore", weekday_goal=2,
    weekend_goal=0, ready_buffer=1, as_of="2026-06-07")
assert weekend["submission_goal"]["target"] == 0


# The explicit collection boundary distinguishes a successfully read empty
# ledger from a missing ledger and retains stable error codes.
with tempfile.TemporaryDirectory() as root:
    tracker_path = os.path.join(root, "applications.yaml")
    queue_path = os.path.join(root, "queue.yaml")
    ledger_path = os.path.join(root, "events.json")
    ingest_path = os.path.join(root, "ingest.json")
    snapshots_path = os.path.join(root, "snapshots")
    os.makedirs(snapshots_path)
    for path, content in ((tracker_path, "[]\n"), (queue_path, "[]\n"),
                          (ledger_path, json.dumps({"schema": 1, "coverage": {
                              "complete": False, "observed_at": NOW.isoformat()}, "events": []})),
                          (ingest_path, '{"schema":1,"scopes":{}}\n')):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    collected = report_snapshot.collect_snapshot(
        tracker_path=tracker_path, queue_path=queue_path, ledger_path=ledger_path,
        ingestion_path=ingest_path, snapshot_root=snapshots_path,
        as_of="2026-02-10", now=NOW)
    assert collected["sources"]["events"]["status"] == "fresh"
    # Touching or recopied ledger bytes cannot refresh the source observation.
    with open(ledger_path, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "coverage": {"complete": False,
                   "observed_at": "2026-01-01T00:00:00Z"}, "events": []}, fh)
    os.utime(ledger_path, (NOW.timestamp(), NOW.timestamp()))
    touched = report_snapshot.collect_snapshot(
        tracker_path=tracker_path, queue_path=queue_path, ledger_path=ledger_path,
        ingestion_path=ingest_path, snapshot_root=snapshots_path,
        as_of="2026-02-10", now=NOW)
    assert touched["sources"]["events"]["status"] == "stale"
    assert touched["sources"]["events"]["observed_at"] == "2026-01-01T00:00:00Z"
    assert touched["sources"]["events"]["last_success_at"] == "2026-02-10T12:00:00Z"
    with open(ledger_path, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "coverage": {}, "events": []}, fh)
    unknown_observation = report_snapshot.collect_snapshot(
        tracker_path=tracker_path, queue_path=queue_path, ledger_path=ledger_path,
        ingestion_path=ingest_path, snapshot_root=snapshots_path,
        as_of="2026-02-10", now=NOW)
    assert unknown_observation["sources"]["events"]["observed_at"] is None
    assert unknown_observation["sources"]["events"]["error_code"] == "observation_time_unknown"
    assert unknown_observation["sources"]["events"]["status"] == "stale"
    os.remove(ledger_path)
    missing = report_snapshot.collect_snapshot(
        tracker_path=tracker_path, queue_path=queue_path, ledger_path=ledger_path,
        ingestion_path=ingest_path, snapshot_root=snapshots_path,
        as_of="2026-02-10", now=NOW)
    assert missing["sources"]["events"]["status"] == "unavailable"
    assert missing["sources"]["events"]["error_code"] == "source_missing"
    with open(ledger_path, "w", encoding="utf-8") as fh:
        fh.write("{malformed")
    malformed = report_snapshot.collect_snapshot(
        tracker_path=tracker_path, queue_path=queue_path, ledger_path=ledger_path,
        ingestion_path=ingest_path, snapshot_root=snapshots_path,
        as_of="2026-02-10", now=NOW)
    assert malformed["sources"]["events"]["status"] == "unavailable"
    assert malformed["sources"]["events"]["error_code"] == "source_invalid"
    with open(ledger_path, "w", encoding="utf-8") as fh:
        fh.write('{"schema":1,"schema":1,"coverage":{},"events":[]}')
    duplicate_keys = report_snapshot.collect_snapshot(
        tracker_path=tracker_path, queue_path=queue_path, ledger_path=ledger_path,
        ingestion_path=ingest_path, snapshot_root=snapshots_path,
        as_of="2026-02-10", now=NOW)
    assert duplicate_keys["sources"]["events"]["status"] == "unavailable"

# Full timestamp precision controls the strict-after test. Receipts are not
# hiring outcomes; dropped malformed rows remain visible in both formats.
precise_queue = [dict(queue[0], submitted_at="2026-01-01T12:00:00.900000Z")]
edge_events = [
    _event("before-fraction", aid_a, "offer", "2026-01-01T12:00:00.500000Z", "human"),
    _event("receipt-only", aid_a, "acknowledgement", "2026-01-02T12:00:00Z", "automated"),
    {"event_type": "offer", "application_id": aid_a,
     "occurred_at": "2026-01-02T12:00:00Z", "verification": "verified"},
]
edge_snapshot = report_snapshot.normalize_snapshot(
    [tracker[0]], precise_queue, {"schema": 1, "coverage": {
        "observed_at": NOW.isoformat()}, "events": edge_events},
    {"schema": 1, "scopes": {}}, sources, as_of=AS_OF, generated_at=NOW)
edge_report = funnel_report.build_report(edge_snapshot)
edge_cohort = edge_report["mature_cohorts"][0]
assert edge_cohort["observed_numerator"]["offer"] == 0
assert edge_cohort["observed_numerator"]["other"] == 0
assert edge_cohort["response_timing_days"]["other"]["n"] == 0
assert edge_report["identity_diagnostics"]["effective_event_diagnostics"]["events_missing_event_id"] == 1
assert '"events_missing_event_id": 1' in funnel_report.render_text(edge_report)
after_fraction = _event("after-fraction", aid_a, "offer", "2026-01-01T12:00:00.950000Z", "human")
after_snapshot = report_snapshot.normalize_snapshot(
    [tracker[0]], precise_queue, {"schema": 1, "coverage": {
        "observed_at": NOW.isoformat()}, "events": [after_fraction]},
    {"schema": 1, "scopes": {}}, sources, as_of=AS_OF, generated_at=NOW)
assert funnel_report.build_report(after_snapshot)["mature_cohorts"][0]["observed_numerator"]["offer"] == 1


with mock.patch("pipeline_health.main", return_value=0) as health_main:
    assert summary.main(["health", "--as-of", "2026-06-03", "--json",
                         "--weekday-goal", "3"]) == 0
    health_args = health_main.call_args[0][0]
    assert "--weekday-goal" in health_args and "3" in health_args
with mock.patch("funnel_report.main", return_value=0) as funnel_main, \
        mock.patch("jev_ask.ask", side_effect=AssertionError("report mode must bypass Jev")):
    assert gate_summary.main(["funnel", "--as-of", "2026-06-03", "--json"]) == 0
    assert "--json" in funnel_main.call_args[0][0]

print("test_reports: ok")
