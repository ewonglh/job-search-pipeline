"""Synthetic, offline coverage for the cached analytics contract."""
import json
import os
import tempfile

import activity_analytics
import projection


def _app(app_id, company, claims, stage="applied"):
    return {"application_id": app_id, "row": {"company": company, "status": stage},
            "submission_date_claims": claims}


def _source(coverage="complete"):
    return {name: {"status": "stale", "coverage_status": coverage}
            for name in ("tracker", "queue", "snapshots", "activities")}


def _snapshot():
    app = _app("pr:1", "Acme", [
        {"value": "2026-01-05", "source": "tracker"},
        {"value": "2026-01-05", "source": "queue"},
    ], "tailored")
    conflict = _app("pr:2", "Twin", [
        {"value": "2026-01-04", "source": "tracker"},
        {"value": "2026-01-06", "source": "queue"},
    ])
    return {"as_of": "2026-01-06", "sources": _source(), "facts": {
        "applications": [app, dict(app), conflict],
        "confirmed_submissions": [
            {"application_id": "pr:1", "attempt_id": "a1", "submitted_at": "2026-01-04T16:00:00Z"},
            {"application_id": "pr:1", "attempt_id": "a2", "submitted_at": "2026-01-05"},
        ],
        "activities": [
            {"activity_id": "t1", "application_id": "pr:1", "kind": "initial_tailoring",
             "completed_at": "2026-01-04T16:00:00Z"},
            {"activity_id": "t1-copy", "application_id": "pr:1", "kind": "initial_tailoring",
             "completed_at": "2026-01-05"},
            {"activity_id": "r1", "application_id": "pr:1", "kind": "retailoring",
             "completed_at": "2026-01-05"},
            {"activity_id": "r2", "application_id": "pr:1", "kind": "retailoring",
             "completed_at": "2026-01-06"},
        ], "activity_coverage": {"complete": True}, "identity_conflicts": []}}


mixed_claims = _snapshot()
mixed_claims["facts"]["applications"] = [mixed_claims["facts"]["applications"][0]]
mixed_claims["facts"]["applications"][0]["submission_date_claims"].append({"missing": True})
mixed_report = activity_analytics.build_report(mixed_claims, "alltime")
assert mixed_report["metrics"]["recorded_claims"] == "unknown_or_partial"
assert mixed_report["totals"]["recorded_claims"] is None
assert mixed_report["observed_totals"]["recorded_claims"] == 1
assert mixed_report["submission_timing"]["claims_missing_or_invalid"] == 1

snapshot = _snapshot()
report = activity_analytics.build_report(snapshot, "custom", start="2026-01-04", end="2026-01-06")
assert report["timezone"] == "Asia/Singapore"
assert report["applications"]["canonical_count"] == 2
assert report["submission_timing"]["claim_conflicts"] == 1
assert report["totals"]["confirmed_submissions"] == 1
assert report["totals"]["recorded_claims"] is None
assert report["observed_totals"]["recorded_claims"] == 1
assert report["totals"]["initial_tailoring"] == 1
assert report["totals"]["retailoring"] == 2
assert report["current_stage_counts"] == {"applied": 1, "tailored": 1}
assert report["series"][1]["application_ids"]["confirmed_submissions"] == ["pr:1"]

# Group tracker/queue spellings before counting, while retaining operational
# statuses that are not part of the recruitment stage vocabulary.
stage_snapshot = _snapshot()
stage_snapshot["facts"]["applications"] = [
    _app("pr:{}".format(index), "Fixture Co", [], status)
    for index, status in enumerate(
        ("Rejected", "rejected", "Applied", "submitted", "Failed", "failed",
         "skipped", "outcome_unknown", "online_assessment"), 20)
]
stage_snapshot["facts"]["applications"][3]["row"]["stage"] = "unknown"
stage_snapshot["facts"]["applications"][0]["row"].update(stage="Rejected", status="applied")
stage_report = activity_analytics.build_report(stage_snapshot)
assert stage_report["current_stage_counts"] == {
    "applied": 2, "failed": 2, "oa": 1, "outcome_unknown": 1,
    "rejected": 2, "skipped": 1,
}
assert sum(stage_report["current_stage_counts"].values()) == stage_report["applications"]["canonical_count"]

# Singapore midnight changes the Monday weekly bin exactly at 16:00 UTC Sunday.
edge = _snapshot()
edge["facts"]["activities"] = [
    {"activity_id": "before", "application_id": "pr:1", "kind": "initial_tailoring",
     "completed_at": "2026-01-04T15:59:59Z"},
    {"activity_id": "after", "application_id": "pr:1", "kind": "retailoring",
     "completed_at": "2026-01-04T16:00:00Z"},
]
edge["as_of"] = "2026-01-05"
weekly = activity_analytics.build_report(edge, "last12weeks")
before = next(item for item in weekly["series"] if item["key"] == "2025-12-29")
after = next(item for item in weekly["series"] if item["key"] == "2026-01-05")
assert before["observed_values"]["initial_tailoring"] == 1
assert after["observed_values"]["retailoring"] == 1

# Missing/conflicting dates and absent activity coverage do not produce zero claims.
partial = _snapshot()
partial["sources"] = _source("partial")
partial["facts"]["activity_coverage"] = {"complete": False}
partial["facts"]["activities"].append({
    "activity_id": "bad-time", "application_id": "pr:1", "kind": "retailoring",
    "completed_at": "not a date"})
unknown = activity_analytics.build_report(partial, "alltime")
assert unknown["totals"]["recorded_claims"] is None
assert unknown["totals"]["initial_tailoring"] is None
assert unknown["observed_totals"]["initial_tailoring"] == 1
assert unknown["activity_timing"]["activity_time_unknown"] == 1
assert unknown["submission_timing"]["claim_conflicts"] == 1

# Source traces and current stages alone never become tailoring completions.
no_activity = {"as_of": "2026-01-06", "sources": _source(), "facts": {
    "applications": [_app("pr:1", "Acme", [], "tailored")], "traces": {"runs": [{}]}}}
none = activity_analytics.build_report(no_activity, "alltime")
assert none["totals"]["initial_tailoring"] is None
assert none["observed_totals"]["initial_tailoring"] == 0
assert none["current_stage_counts"] == {"tailored": 1}

company_snapshot = _snapshot()
company_snapshot["facts"]["applications"].append(
    _app("pr:3", "Acme", [{"value": "2026-01-05"}]))
catalog = {"schema_version": 1, "records": [{
    "key": "acme-key", "aliases": ["acme"], "size": "SME",
    "footprint": "multinational", "primary_industry": "software"}]}
company_report = activity_analytics.build_report(company_snapshot, "alltime", catalog=catalog)
assert company_report["companies"]["known_company_count"] == 1
assert company_report["companies"]["size"]["SME"] == 1
assert company_report["companies"]["footprint"]["multinational"] == 1
assert company_report["companies"]["unresolved_application_count"] == 1

# Projection accepts only bounded allowlisted activity facts and reports partial
# coverage when any record is malformed.
with tempfile.TemporaryDirectory() as temp:
    ledger = os.path.join(temp, "activities.json")
    with open(ledger, "w", encoding="utf-8") as handle:
        json.dump({"schema": 1, "coverage": {"complete": True}, "activities": [
            {"activity_id": "safe-1", "application_id": "pr:1",
             "kind": "initial_tailoring", "completed_at": "2026-01-05",
             "answers": {"private": "discard"}},
            {"activity_id": "bad", "application_id": "pr:1", "kind": "run",
             "completed_at": "2026-01-05"},
        ]}, handle)
    loaded = projection._read_activities(ledger)
    assert loaded["coverage_status"] == "partial"
    assert loaded["records"] == [{"activity_id": "safe-1", "application_id": "pr:1",
                                   "kind": "initial_tailoring", "completed_at": "2026-01-05"}]

print("test_activity_analytics: ok")
