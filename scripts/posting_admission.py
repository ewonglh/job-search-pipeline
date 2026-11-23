"""Freshness rules used before readiness and queue admission."""
from datetime import date, datetime

from config import int_setting


def freshness_exclusion(posting, today=None):
    """Return a reason when the recorded discovery date exceeds the age cap."""
    limit = int_setting("JOBSEARCH_MAX_AGE_DAYS", 7)
    if not limit:
        return None
    if limit < 0:
        raise ValueError("JOBSEARCH_MAX_AGE_DAYS must be zero or positive")
    added = str(posting.get("added") or "").strip()
    if not added:
        return None
    try:
        added_day = date.fromisoformat(added[:10])
    except ValueError as exc:
        raise ValueError("posting discovery date is malformed") from exc
    if ((today or datetime.now().date()) - added_day).days > limit:
        return f"posting discovered {added_day} is older than {limit} days"
    return None
