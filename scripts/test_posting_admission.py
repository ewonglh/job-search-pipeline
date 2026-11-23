#!/usr/bin/env python3
"""Synthetic checks for pure, run-date-bound posting freshness rules."""
from datetime import date
from unittest import mock

import posting_admission as admission


POSTING = {"added": "2026-11-15"}
RUN_DATE = date(2026, 11, 23)

# An explicit limit and date make the admission rule pure and repeatable.
assert "older than 7 days" in admission.freshness_exclusion(
    POSTING, today=RUN_DATE, max_age_days=7)
assert admission.freshness_exclusion(
    POSTING, today=RUN_DATE, max_age_days=8) is None
assert admission.freshness_exclusion(
    POSTING, today=RUN_DATE, max_age_days=0) is None

# Existing callers can omit the new limit and retain their old entry point.
with mock.patch.object(admission, "int_setting", return_value=7):
    assert admission.freshness_exclusion(POSTING, RUN_DATE)

# Configuration errors are ordinary validation failures, not a per-row exit.
with mock.patch.object(admission, "int_setting",
                       side_effect=SystemExit("invalid age setting")):
    try:
        admission.configured_max_age_days()
        raise AssertionError("invalid configuration was accepted")
    except ValueError as exc:
        assert "invalid age setting" in str(exc)

print("test_posting_admission: explicit age, run date, and configuration passed")
