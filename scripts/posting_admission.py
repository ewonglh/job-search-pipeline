"""Freshness rules used before readiness and queue admission."""

from datetime import date, datetime
import urllib.parse


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
        added_day = date.fromisoformat(added)
    except ValueError as exc:
        raise ValueError("posting discovery date is malformed") from exc
    if ((today or date.today()) - added_day).days > limit:
        return f"posting discovered {added_day} is older than {limit} days"
    return None


def owner_control_exclusion(posting, skipped=(), gates=(), today=None):
    """Return a reason when an owner skip or active employer cap blocks a posting."""
    import application_identity as identity

    link = identity.normalize_posting_url(posting.get("link"))
    company = str(posting.get("company") or "").strip().casefold()
    role = str(posting.get("role") or "").strip().casefold()
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        skip_link = identity.normalize_posting_url(entry.get("link"))
        skip_company = str(entry.get("company") or "").strip().casefold()
        skip_role = str(entry.get("role") or "").strip().casefold()
        if ((skip_link and skip_link == link)
                or (skip_company and skip_role
                    and (skip_company, skip_role) == (company, role))):
            return "posting is on the owner's skip list"

    today_value = (today or datetime.now().date()).isoformat()
    host = urllib.parse.urlsplit(str(posting.get("link") or "")).netloc.lower()
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        until = str(gate.get("gate_until") or "").strip()
        if until:
            try:
                datetime.strptime(until, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("owner cap has an invalid gate_until") from exc
            if until < today_value:
                continue
        aliases = gate.get("company_aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        cap_names = {str(name).strip().casefold() for name in aliases
                     if str(name).strip()}
        cap_host = str(gate.get("host") or "").strip().lower().lstrip(".")
        if ((company and company in cap_names)
                or (cap_host and (host == cap_host or host.endswith("." + cap_host)))):
            return "an owner application cap is active for this posting"
    return None


def admission_exclusion(posting, skipped=(), gates=(), explicitly_closed=False,
                        today=None):
    """Shared final admission check for freshness, owner controls and closure."""
    reason = freshness_exclusion(posting, today=today)
    if reason:
        return reason
    reason = owner_control_exclusion(posting, skipped, gates, today=today)
    if reason:
        return reason
    if explicitly_closed:
        return "current posting is explicitly closed"
    return None
