"""Freshness rules used before readiness and queue admission."""
from datetime import date

from config import int_setting


def configured_max_age_days():
    """Read and validate the command's posting age limit once at its boundary."""
    try:
        limit = int_setting("JOBSEARCH_MAX_AGE_DAYS", 7)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    if limit < 0:
        raise ValueError("configuration error: JOBSEARCH_MAX_AGE_DAYS must be zero or positive")
    return limit


def freshness_exclusion(posting, today=None, max_age_days=None):
    """Return a reason when the recorded discovery date exceeds the age cap."""
    limit = configured_max_age_days() if max_age_days is None else max_age_days
    if type(limit) is not int or limit < 0:
        raise ValueError("JOBSEARCH_MAX_AGE_DAYS must be a non-negative integer")
    if not limit:
        return None
    added = str(posting.get("added") or "").strip()
    if not added:
        return None
    try:
        added_day = date.fromisoformat(added[:10])
    except ValueError as exc:
        raise ValueError("posting discovery date is malformed") from exc
    if ((today or date.today()) - added_day).days > limit:
        return f"posting discovered {added_day} is older than {limit} days"
    return None
