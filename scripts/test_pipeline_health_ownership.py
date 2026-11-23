#!/usr/bin/env python3
"""Synthetic checks for queue-backed, sanitized health ownership."""
import json
import os
import tempfile
from datetime import datetime

import pipeline_health
import projection
import queue as queue_module
import report_snapshot


NOW = "2026-11-23T06:00:00Z"


def _owned(status, number, *, legacy=False):
    row = {
        "pr_number": number,
        "status": status,
        "claimed_at": "2026-11-23T04:00:00Z",
        "attempt_id": "attempt-%s" % number,
    }
    if not legacy:
        row.update({
            "browser_owner_attempt_id": "attempt-%s" % number,
            "runner_id": "runner-%s" % number,
            "session_ref": "session-private-%s" % number,
            "lease_expires_at": "2026-11-24T04:00:00Z",
        })
    return row


def _safe_row(row):
    owner = report_snapshot.queue_owner_projection(row)
    return {
        "pr_number": row["pr_number"],
        "status": row["status"],
        "claimed_at": row.get("claimed_at"),
        "browser_ownership_status": owner["status"],
        "browser_ownership_legacy": "true" if owner["legacy"] else "false",
    }


def _health(row):
    return pipeline_health.build_report({
        "as_of": "2026-11-23",
        "generated_at": NOW,
        "sources": {"tracker": {"status": "fresh"},
                    "queue": {"status": "fresh"}},
        "facts": {"applications": [], "attempts": [row]},
    }, timezone_name="UTC", weekday_goal=1, weekend_goal=0,
       ready_buffer=1, as_of="2026-11-23")


def _check_case(row, expected_status, expected_legacy=False):
    queue_error = False
    try:
        queue_owner = queue_module._browser_owner_from_rows([row])
    except queue_module.QueueStateError:
        queue_owner = None
        queue_error = True
    actual_active = queue_owner is not None
    projection_owner = report_snapshot.queue_owner_projection(row)
    assert projection_owner["status"] == expected_status
    assert projection_owner["legacy"] is expected_legacy
    assert actual_active == (expected_status == "active")
    assert queue_error == (expected_status == "unknown")

    report = _health(_safe_row(row))
    ownership = report["browser_ownership"]
    assert ownership["active"] == int(actual_active)
    assert ownership["released"] == int(expected_status == "released")
    assert ownership["unknown"] == int(expected_status == "unknown")
    assert len(ownership["owners"]) == int(actual_active)
    serialized = json.dumps(report)
    for private_value in ("runner-", "session-private-", "attempt-"):
        assert private_value not in serialized
    if actual_active:
        owner = ownership["owners"][0]
        assert owner["application_status"] == row["status"]
        assert owner["legacy"] is expected_legacy
        assert owner["pr_number"] == row["pr_number"]
        assert "runner_id" not in owner
        assert "session_ref" not in owner
        assert "attempt_id" not in owner


def main():
    cases = [
        (_owned("in_progress", 901), "active", False),
        (_owned("blocked", 902), "active", False),
        (_owned("submitted", 903), "active", False),
        ({"pr_number": 904, "status": "submitted",
          "attempt_id": "attempt-904",
          "browser_owner_released_attempt_id": "attempt-904",
          "browser_owner_released_at": "2026-11-23T05:00:00Z"}, "released", False),
        (_owned("in_progress", 905, legacy=True), "active", True),
        ({"pr_number": 906, "status": "in_progress",
          "browser_owner_attempt_id": "attempt-906",
          "runner_id": "runner-906", "lease_expires_at": ""}, "unknown", False),
    ]
    for row, status, legacy in cases:
        _check_case(row, status, legacy)

    normalized = report_snapshot.normalize_snapshot(
        [], [_owned("submitted", 908)],
        {"schema": 1, "coverage": {"complete": False}, "events": []},
        {"schema": 1, "scopes": {}},
        {"tracker": {"status": "fresh"}, "queue": {"status": "fresh"}},
        as_of="2026-11-23",
        generated_at=datetime.fromisoformat(NOW.replace("Z", "+00:00")))
    normalized_attempt = normalized["facts"]["attempts"][0]
    assert normalized_attempt["browser_ownership_status"] == "active"
    assert normalized_attempt["browser_ownership_legacy"] == "false"
    assert "runner_id" not in normalized_attempt
    assert "session_ref" not in normalized_attempt
    assert "browser_owner_attempt_id" not in normalized_attempt
    normalized_text = json.dumps(normalized["facts"])
    for private_value in ("runner-908", "session-private-908"):
        assert private_value not in normalized_text

    queue_text = "\n".join((
        "- pr_number: 907",
        "  status: submitted",
        "  attempt_id: attempt-canary",
        "  browser_owner_attempt_id: attempt-canary",
        "  runner_id: runner-canary",
        "  session_ref: session-canary",
        "  lease_expires_at: 2026-11-24T04:00:00Z",
        "  claimed_at: 2026-11-23T04:00:00Z",
    )) + "\n"
    with tempfile.TemporaryDirectory() as temp_dir:
        queue_path = os.path.join(temp_dir, "queue.yaml")
        with open(queue_path, "w", encoding="utf-8") as stream:
            stream.write(queue_text)
        projected = projection._read_queue(queue_path)
    assert projected["error_code"] is None
    assert projected["records"][0]["browser_ownership_status"] == "active"
    assert projected["records"][0]["browser_ownership_legacy"] == "false"
    projected_text = json.dumps(projected["records"])
    for private_value in ("runner-canary", "session-canary"):
        assert private_value not in projected_text
    for field in ("runner_id", "session_ref", "browser_owner_attempt_id",
                  "browser_owner_released_attempt_id"):
        assert field not in projected["records"][0]
    assert projected["records"][0]["attempt_id"] == "attempt-canary"

    print("test_pipeline_health_ownership: passed %d scenarios" % len(cases))


if __name__ == "__main__":
    main()
