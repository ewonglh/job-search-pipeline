"""Offline synthetic tests for cached dashboard chart selection and states."""
import application_explorer
import dashboard
import dashboard_charts
from test_dashboard_views import synthetic_snapshot


def test_metric_bins_use_singapore_dates_canonical_ids_and_render_exact_rows():
    snapshot = synthetic_snapshot()
    snapshot["facts"]["confirmed_submissions"] = [
        {"application_id": "app-alpha", "submitted_at": "2026-11-16T15:59:00Z"},
        {"application_id": "app-alpha", "submitted_at": "2026-11-16T15:59:00Z"},
        {"application_id": "app-beta", "submitted_at": "2026-11-16T16:01:00Z"},
    ]
    report = dashboard_charts.build_report(snapshot, "custom", "2026-11-16", "2026-11-16")
    assert dashboard_charts.metric_application_ids(
        report, "confirmed_submissions", "2026-11-16", "2026-11-16") == ["app-alpha"]
    state = application_explorer.parse_query(
        "metric=confirmed_submissions&from=2026-11-16&to=2026-11-16")
    rows = dashboard._application_rows(snapshot)
    selected, note = dashboard_charts.filter_applications(snapshot, rows, state)
    assert [row["company"] for row in selected] == ["Acme & Co"]
    assert "1 canonical applications" in note
    analytics = dashboard.render_snapshot_html(
        snapshot, page="analytics", query_state=application_explorer.parse_query("period=alltime"))
    assert "<polyline" in analytics


def test_company_dimensions_count_independent_denominators_and_filter_by_key():
    snapshot = synthetic_snapshot()
    snapshot["facts"]["company_catalog"] = {"schema_version": 1, "records": [
        {"key": "acme", "aliases": ["acme & co"], "size": "SME",
         "footprint": "domestic", "primary_industry": "AI"},
        {"key": "beta", "aliases": ["beta ltd"], "size": "large",
         "footprint": "multinational", "primary_industry": "QA"},
    ]}
    view = dashboard_charts.build_view(snapshot, "alltime")
    size = {row["label"]: row for row in view["companies"]["dimensions"]["size"]}
    assert size["SME"] == {"label": "SME", "companies": 1, "applications": 1}
    assert size["large"] == {"label": "large", "companies": 1, "applications": 1}
    assert view["companies"]["unresolved_applications"] == 1
    one_day = dashboard_charts.build_view(snapshot, "custom", "2026-11-16", "2026-11-16")
    selected_size = {row["label"]: row for row in one_day["companies"]["dimensions"]["size"]}
    assert selected_size == {"large": {"label": "large", "companies": 1, "applications": 1}}
    state = application_explorer.parse_query("period=alltime&company=acme")
    selected, _note = dashboard_charts.filter_applications(
        snapshot, dashboard._application_rows(snapshot), state)
    assert [row["company"] for row in selected] == ["Acme & Co"]


def test_malformed_query_and_range_form_are_bounded_and_get_only():
    for query in ("metric=unknown&from=2026-01-01&to=2026-01-02",
                  "metric=confirmed_submissions&from=2026-02-30&to=2026-03-01",
                  "period=custom&start=2026-02-02&end=2026-02-01",
                  "company=%3Cscript%3E"):
        try:
            application_explorer.parse_query(query)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted hostile query: {}".format(query))
    state = application_explorer.parse_query("")
    rendered = dashboard.render_snapshot_html(
        synthetic_snapshot(), page="analytics", query_state=state)
    assert "method='get' action='/jobs/analytics/'" in rendered
    assert "Monthly confirmed submission trend" in rendered
    assert "mature cohort" in rendered


def test_partial_unavailable_stale_and_genuine_zero_are_distinct():
    snapshot = synthetic_snapshot()
    partial = dashboard.render_snapshot_html(snapshot, page="home")
    assert "partial coverage" in partial.casefold() and "not a confirmed zero" in partial
    zero_snapshot = synthetic_snapshot()
    zero_snapshot["facts"]["confirmed_submissions"] = []
    genuine_zero = dashboard.render_snapshot_html(zero_snapshot, page="home")
    assert "genuine zero observations" in genuine_zero
    stale_snapshot = synthetic_snapshot()
    stale_snapshot["facts"]["confirmed_submissions"] = []
    stale_snapshot["sources"]["snapshots"]["status"] = "stale"
    stale = dashboard.render_snapshot_html(stale_snapshot, page="home")
    assert "last observed values" in stale
    unavailable_snapshot = synthetic_snapshot()
    unavailable_snapshot["sources"]["snapshots"]["status"] = "unavailable"
    unavailable = dashboard.render_snapshot_html(unavailable_snapshot, page="home")
    assert "metric is unavailable" in unavailable and "no zero is inferred" in unavailable


if __name__ == "__main__":
    for test in (test_metric_bins_use_singapore_dates_canonical_ids_and_render_exact_rows,
                 test_company_dimensions_count_independent_denominators_and_filter_by_key,
                 test_malformed_query_and_range_form_are_bounded_and_get_only,
                 test_partial_unavailable_stale_and_genuine_zero_are_distinct):
        test()
    print("test_dashboard_charts: all assertions passed")
