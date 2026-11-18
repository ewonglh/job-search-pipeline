"""Synthetic boundedness and query-contract checks for application explorer."""
import copy
import re

import application_explorer as explorer
import dashboard as d
from test_dashboard_views import synthetic_snapshot


def many_snapshot():
    snapshot = synthetic_snapshot()
    stages = explorer.STAGES
    snapshot["facts"]["applications"] = []
    for index in range(270):
        stage = stages[index % len(stages)]
        app_id = "synthetic-application-{:03d}".format(index)
        snapshot["facts"]["applications"].append({
            "application_id": app_id,
            "row": {"company": "Company {:03d}".format(index),
                    "role": "Synthetic role", "source": "Synthetic board",
                    "stage": stage, "applied_date": "2026-11-01",
                    "link": "https://example.invalid/jobs/{}".format(index)},
            "source": "tracker", "submission_date_claims": [],
        })
    snapshot["facts"]["attempts"] = []
    snapshot["facts"]["events"] = []
    snapshot["facts"]["application_aliases"] = {}
    return snapshot


def test_search_sort_filter_and_paging_compose_for_late_records():
    snapshot = many_snapshot()
    data = d.build_snapshot_data(snapshot)
    assert len(data["applications"]) == 270
    assert all("timeline" not in row for row in data["applications"])
    state = explorer.parse_query("q=Company+269&stage=Unknown+%2F+unmatched&sort=role&dir=desc&page=9")
    matches = explorer.filtered_rows(data["applications"], state)
    assert [row["company"] for row in matches] == ["Company 269"]
    late_page = explorer.filtered_rows(data["applications"], explorer.parse_query("sort=company&page=11"))
    assert late_page[(11 - 1) * 25]["company"] == "Company 250"
    recent = explorer.filtered_rows(
        [{"company": "A", "applied_date": "2026-11-10"}, {"company": "Z", "applied_date": "2026-11-12"}],
        explorer.parse_query(""))
    assert recent[0]["company"] == "Z"
    key = matches[0]["application_key"]
    detail = d._application_detail(snapshot, key)
    assert detail["company"] == "Company 269"
    assert detail["next_action"] == "No next action recorded"


def test_first_render_and_independent_board_lanes_are_bounded():
    snapshot = many_snapshot()
    data = d.build_snapshot_data(snapshot)
    listing = d.render_snapshot_html(snapshot, data=data, page="applications")
    # The phone card carries its own detail link in addition to the title link.
    assert len(re.findall(r"href='/jobs/applications/[0-9a-f]{64}'", listing)) == 50
    assert listing.count("Next page</a>") == 2
    state = explorer.parse_query("view=board&load_applied=20")
    board = d.render_snapshot_html(snapshot, data=data, page="applications", query_state=state)
    assert len(re.findall(r"href='/jobs/applications/[0-9a-f]{64}'", board)) == 100
    assert "No applications in this stage" not in board
    assert "load_found=20" in board and "load_applied=30" in board


def test_details_are_opaque_safe_and_never_choose_ambiguous_rows():
    snapshot = many_snapshot()
    app = snapshot["facts"]["applications"][0]
    key = explorer.opaque_key(app["application_id"])
    app["row"]["link"] = "javascript:alert(1)"
    detail = d._application_detail(snapshot, key)
    assert detail["link"] is None
    rendered = d.render_snapshot_html(snapshot, page="application-detail", application_key=key)
    assert "javascript:" not in rendered and app["application_id"] not in rendered
    snapshot["facts"]["applications"].append(copy.deepcopy(app))
    assert d._application_detail(snapshot, key) == "ambiguous"
    assert d._application_detail(snapshot, "f" * 64) is None


def test_malformed_or_hostile_query_inputs_are_bounded():
    for query in ("page=999999", "stage=<script>", "view=tree", "sort=__import__",
                  "q=one&q=two", "unknown=x", "page=2&", "load_applied=11",
                  "q=" + "x" * 121):
        try:
            explorer.parse_query(query)
        except ValueError:
            continue
        raise AssertionError("accepted malformed query: {!r}".format(query))
    state = explorer.parse_query("q=%3Cscript%3E%26&stage=all")
    assert state["q"] == "<script>&"


def main():
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print("  ok  {}".format(name))
    print("test_application_explorer: all assertions passed")


if __name__ == "__main__":
    main()
