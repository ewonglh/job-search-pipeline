#!/usr/bin/env python3
"""Synthetic cached-view, parity, age, and escaping checks for dashboard.py."""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
from unittest import mock

import dashboard as d
import funnel_report
import pipeline_health
import projection


AS_OF = "2026-11-16"


def synthetic_snapshot():
    return {
        "schema_version": 1,
        "snapshot_id": "a" * 64,
        "generated_at": "2026-11-16T00:00:00Z",
        "as_of": AS_OF,
        "sources": {
            "tracker": {"status": "fresh", "observed_at": "2026-11-16T00:00:00Z",
                        "collected_at": "2026-11-16T00:00:00Z", "age_seconds": 0,
                        "freshness_seconds": 86400, "error_code": None},
            "queue": {"status": "stale", "observed_at": "2026-11-10T00:00:00Z",
                      "collected_at": "2026-11-16T00:00:00Z", "age_seconds": 518400,
                      "freshness_seconds": 86400, "error_code": "source_read_failed",
                      "last_success_at": "2026-11-10T00:00:00Z"},
            "events": {"status": "stale", "observed_at": "2026-10-28T00:00:00Z",
                       "collected_at": "2026-11-16T00:00:00Z", "age_seconds": 1641600,
                       "freshness_seconds": 604800, "error_code": "source_timeout"},
            "ingestion": {"status": "unavailable", "observed_at": None,
                          "collected_at": "2026-11-16T00:00:00Z", "age_seconds": None,
                          "error_code": "source_missing"},
            "snapshots": {"status": "fresh", "observed_at": "2026-11-16T00:00:00Z",
                          "age_seconds": 0, "error_code": None},
            "reviews": {"status": "fresh", "observed_at": "2026-11-16T00:00:00Z",
                        "age_seconds": 0, "error_code": None},
            "preflight": {"status": "fresh", "observed_at": "2026-11-16T00:00:00Z",
                          "age_seconds": 0, "error_code": None},
            "traces": {"status": "fresh", "observed_at": "2026-11-16T00:00:00Z",
                       "age_seconds": 0, "error_code": None},
        },
        "facts": {
            "applications": [
                {
                    "application_id": "app-alpha",
                    "row": {"company": "Acme & Co", "role": "Backend <script>role</script>",
                            "source": "Board <One>", "stage": "Interview",
                            "stage_since": "2026-11-10", "applied_date": "2026-10-20",
                            "link": "https://example.invalid/jobs/alpha"},
                    "submission_date_claims": [{"source": "tracker", "value": "2026-10-20",
                                                "invalid": False, "missing": False}],
                    "submission_moments": [], "resume_hash": "b" * 64,
                    "queue_rows": [],
                },
                {
                    "application_id": "app-beta",
                    "row": {"company": "Beta Ltd", "role": "QA Intern", "source": "Agency & Co",
                            "stage": "Tailored", "applied_date": AS_OF,
                            "link": "https://example.invalid/jobs/beta"},
                    "submission_date_claims": [{"source": "tracker", "value": AS_OF,
                                                "invalid": False, "missing": False}],
                    "submission_moments": [], "resume_hash": None,
                    "queue_rows": [],
                },
                {
                    "application_id": "app-unknown",
                    "row": {"company": "<img src=x onerror=alert(1)>",
                            "role": "Unknown role", "status": "new_status",
                            "link": "javascript:alert(1)"},
                    "submission_date_claims": [], "submission_moments": [],
                    "resume_hash": None, "queue_rows": [],
                },
            ],
            "attempts": [
                {"application_id": "app-alpha", "status": "in_progress",
                 "claimed_at": "2026-11-15T12:00:00Z"},
                {"application_id": "app-beta", "status": "queued"},
                {"application_id": None, "status": "outcome_unknown",
                 "company": "Loose attempt", "role": "No matched application",
                 "blocker_code": "submission_ambiguous"},
                {"application_id": "app-unknown", "status": "failed",
                 "blocker_code": "manual_review"},
            ],
            "application_aliases": {"app-alpha": "app-alpha", "alpha-alias": "app-alpha",
                                     "app-beta": "app-beta", "app-unknown": "app-unknown"},
            "conflicted_application_aliases": [],
            "identity_conflicts": [{"reason": "ambiguous_identity", "row_refs": []}],
            "identity_diagnostics": {"unmatched": 1},
            "events": [{"event_id": "event-alpha", "event_type": "recruiter_reply",
                        "application_id": "alpha-alias", "occurred_at": "2026-10-28T09:00:00Z",
                        "verification": "verified", "origin": "human", "raw_body": "MAIL_BODY_CANARY"}],
            "event_coverage": {"complete": False, "observed_at": "2026-10-28T00:00:00Z"},
            "event_diagnostics": {}, "quarantined_event_count": 0,
            "ingestion": {},
            "review_quality": {
                "schema_version": 1, "unique_artifact_heads": 2,
                "first_pass": {
                    "model": {"accepted": 1, "not_accepted": 0, "unknown": 1},
                    "owner": {"accepted": 1, "not_accepted": 1, "unknown": 0},
                },
                "rework_by_code": {"layout_issue": 1},
                "unknown_head_records": 1,
                "records": [
                    {"artifact_head": "c" * 64, "provenance_status": "unknown"},
                    {"artifact_head": "d" * 64, "provenance_status": "verified"},
                ],
                "interpretation": "Model and owner acceptance remain separate.",
            },
            "traces": {
                "collection": {"status": "complete"},
                "runs": [{
                    "job_id": "job-alpha", "run_id": "run-alpha",
                    "execution_result": "succeeded", "completion_status": "succeeded",
                    "business_result": "unknown", "application_join_status": "matched",
                    "artifact_join_status": "unknown", "delivery_status": "unknown",
                    "tool_trace_status": "complete", "attention_codes": ["business_result_unknown"],
                    "tool_events": [{"name": "browser.open", "started_at": "2026-11-15T09:00:00Z",
                                     "finished_at": "2026-11-15T09:00:02Z", "duration_ms": 2000,
                                     "result": "succeeded", "retry_context": "none",
                                     "raw_arguments": "OTP-123-CANARY",
                                     "raw_output": "PRIVATE_TOOL_OUTPUT_CANARY"}],
                }],
                "cost_status": "unavailable",
            },
        },
    }


def test_viewmodel_uses_shared_reports_and_snapshot_identity():
    snapshot = synthetic_snapshot()
    data = d.build_snapshot_data(snapshot, timezone_name="Asia/Singapore")
    expected_today = pipeline_health.build_report(
        snapshot, timezone_name="Asia/Singapore", weekday_goal=1,
        weekend_goal=0, ready_buffer=1, as_of=AS_OF)
    expected_funnel = funnel_report.build_report(
        snapshot, as_of=AS_OF, timezone_name="Asia/Singapore")
    assert data["snapshot_id"] == snapshot["snapshot_id"]
    assert data["compact"] == projection.format_compact(snapshot)
    assert data["today"] == expected_today
    for cohort, expected in zip(data["funnel"]["mature_cohorts"],
                                expected_funnel["mature_cohorts"]):
        for key in ("eligible_denominator", "observed_numerator", "outcome_rates",
                    "exclusions", "coverage", "event_source_status"):
            assert cohort.get(key) == expected.get(key)
    assert "grouped_observed_rates" not in data["funnel"]["mature_cohorts"][0]
    serialized = json.dumps(data)
    assert ("b" * 64) not in serialized and ("c" * 64) not in serialized


def test_age_basis_unknown_status_and_current_status_sankey():
    snapshot = synthetic_snapshot()
    data = d.build_snapshot_data(snapshot)
    cards = {card["company"]: card for card in data["applications"]}
    alpha = cards["Acme & Co"]
    beta = cards["Beta Ltd"]
    unknown = cards["<img src=x onerror=alert(1)>"]
    assert (alpha["days"], alpha["age_from"]) == (6, "stage_since")
    assert alpha["age_label"] == "6 days in interview · stage since 2026-11-10"
    assert beta["age_from"] == "applied_date"
    assert "since applied date" in beta["age_label"]
    assert unknown["status"] == "Unknown / unmatched"
    assert unknown["age_label"] == "No date recorded"
    assert unknown["link"] is None

    sankey = data["sankey"]
    by_flow = {(flow["from"], flow["to"]): flow["count"] for flow in sankey["flows"]}
    assert by_flow[("Board <One>", "Interview")] == 1
    assert by_flow[("Agency & Co", "Tailored")] == 1
    assert by_flow[("Unknown source", "Unknown / unmatched")] == 1
    assert sankey["total"] == 3

    legacy = d.build_sankey(
        [{"source": "Synthetic", "link": "https://example.invalid/orphan"}],
        {}, status_of={})
    assert legacy["flows"] == [{"from": "Other", "to": "Unknown/unmatched", "count": 1}]
    assert "Applied" not in legacy["right"]


def test_stale_blocked_and_unknown_states_remain_visible():
    data = d.build_snapshot_data(synthetic_snapshot())
    today = data["today"]
    assert today["submission_goal"]["confirmed"] == 1
    assert today["submission_goal"]["submissions"]["coverage"] == "stale"
    assert today["ready_buffer"]["queued"] == 1
    assert today["attempts"]["outcome_unknown"] == 1
    assert today["blockers"]["by_code"] == {
        "manual_review": 1, "submission_ambiguous": 1}
    sources = data["sources"]
    assert sources["queue"]["status"] == "stale"
    assert sources["queue"]["error_code"] == "source_read_failed"
    assert sources["ingestion"]["status"] == "unavailable"
    assert data["funnel"]["mature_cohorts"][0]["eligible_denominator"] == 1
    assert data["funnel"]["mature_cohorts"][0]["coverage"]["status"] == "partial"


def test_render_escapes_html_svg_attributes_and_embedded_json():
    snapshot = synthetic_snapshot()
    snapshot["snapshot_id"] = "</script><script>alert('json-breakout')</script>"
    rendered = d.render_snapshot_html(snapshot)
    assert "Acme &amp; Co" in rendered
    assert "Backend &lt;script&gt;role&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert "Board &lt;One&gt;" in rendered
    assert "href='javascript:" not in rendered
    assert "</script><script>alert('json-breakout')" not in rendered
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in rendered
    for canary in ("MAIL_BODY_CANARY", "OTP-123-CANARY", "PRIVATE_TOOL_OUTPUT_CANARY"):
        assert canary not in rendered
    assert "Tailoring quality" in rendered and "Hiring funnel" in rendered
    assert "Current status" in rendered
    assert "not a history of stage transitions" in rendered

    safe_json = d._safe_json_response({"value": "</script><img src=x>"})
    assert "</script>" not in safe_json and "<img" not in safe_json
    assert json.loads(safe_json) == {"value": "</script><img src=x>"}


def test_stage_since_projection_allowlist_is_typed():
    row = {"company": "Synthetic", "role": "Engineer", "status": "interview",
           "stage_since": "2026-11-10"}
    cleaned = projection._strict_rows([row], projection.TRACKER_FIELDS)
    assert cleaned[0]["stage_since"] == "2026-11-10"
    try:
        projection._strict_rows([dict(row, stage_since="not-a-date")],
                                projection.TRACKER_FIELDS)
    except projection._InvalidSource:
        pass
    else:
        raise AssertionError("invalid stage_since was accepted")
    assert d.card_age({"stage_since": "2026-11-10", "applied_date": "2026-10-01"},
                      dt.date(2026, 11, 16)) == (6, "stage_since")


def test_cli_json_and_html_use_cached_snapshot_only():
    snapshot = synthetic_snapshot()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "index.html")
        with mock.patch.object(projection, "read_snapshot", return_value=snapshot), \
                mock.patch.object(d, "build_data", side_effect=AssertionError("legacy collector ran")):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                assert d.main(["--json", "--db", "ignored.sqlite3"]) == 0
            result = json.loads(captured.getvalue())
            assert result["snapshot_id"] == snapshot["snapshot_id"]
            assert result["today"]["submission_goal"]["confirmed"] == 1
            with contextlib.redirect_stdout(io.StringIO()):
                assert d.main(["--out", out, "--db", "ignored.sqlite3"]) == 0
        with open(out, encoding="utf-8") as stream:
            page = stream.read()
        assert "id='today'" in page and "id='applications'" in page
        assert "Snapshot ID" in page


def test_missing_cache_renders_explicit_unavailable_state_without_paths():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "unavailable.html")
        with mock.patch.object(projection, "read_snapshot",
                               side_effect=projection.ProjectionError("SYNTHETIC_PRIVATE_PATH")), \
                mock.patch.object(d, "build_data", side_effect=AssertionError("legacy collector ran")):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                assert d.main(["--out", out]) == 0
            assert "SYNTHETIC_PRIVATE_PATH" not in errors.getvalue()
            captured = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(captured):
                assert d.main(["--json"]) == 2
            assert captured.getvalue() == ""
        with open(out, encoding="utf-8") as stream:
            page = stream.read()
        assert "Cached dashboard unavailable" in page
        assert "SYNTHETIC_PRIVATE_PATH" not in page


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("  ok  {}".format(test.__name__))
    print("test_dashboard_views: all assertions passed")


if __name__ == "__main__":
    main()
