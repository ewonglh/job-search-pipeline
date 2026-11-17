"""Pure analytics over a cached, sanitized report snapshot.

The report never reads source files. Tailoring metrics consume only explicit
``facts.activities`` records with kind initial_tailoring or retailoring. Each
record has ``activity_id``, ``application_id`` and ``completed_at``. A source
may declare ``facts.activity_coverage = {"complete": true}``; absent or
partial coverage remains visibly unknown.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

import company_catalog
import report_snapshot


TZ_NAME = "Asia/Singapore"
SG = report_snapshot.timezone_for(TZ_NAME)
ACTIVITY_KINDS = {"initial_tailoring", "retailoring"}
METRICS = ("confirmed_submissions", "recorded_claims", "initial_tailoring", "retailoring")


def _local_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed_day = date.fromisoformat(value.strip())
        if parsed_day.isoformat() == value.strip():
            return parsed_day
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_day = date.fromisoformat(value.strip())
            return parsed_day if parsed_day.isoformat() == value.strip() else None
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SG).date()


def _date(value, field):
    if value is None:
        return None
    parsed = _local_day(value)
    if parsed is None:
        raise ValueError("{} must be an ISO local date or timezone-aware timestamp".format(field))
    return parsed


def _window(period, as_of, start, end):
    today = _date(as_of, "snapshot.as_of") or datetime.now(SG).date()
    start_day, end_day = _date(start, "start"), _date(end, "end")
    if period == "custom":
        if start_day is None or end_day is None or start_day > end_day:
            raise ValueError("custom period needs valid inclusive start and end dates")
        first, last = start_day, end_day
    elif period == "thisweek":
        first, last = today - timedelta(days=today.weekday()), today
    elif period == "thismonth":
        first, last = today.replace(day=1), today
    elif period == "last3months":
        year, month = today.year, today.month - 2
        while month <= 0:
            year, month = year - 1, month + 12
        first, last = date(year, month, 1), today
    elif period == "alltime":
        return None, today, "month"
    elif period in ("last12weeks", None):
        monday = today - timedelta(days=today.weekday())
        first, last = monday - timedelta(weeks=11), today
        period = "last12weeks"
    else:
        raise ValueError("unsupported analytics period")
    days = (last - first).days + 1
    granularity = ("week" if period in ("last12weeks", "thisweek")
                   else "month" if period in ("thismonth", "last3months")
                   else "month" if days > 90 else "week" if days > 14 else "day")
    return first, last, granularity


def _bucket(day, granularity):
    if granularity == "month":
        return day.replace(day=1)
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    return day


def _next_bucket(day, granularity):
    if granularity == "day":
        return day + timedelta(days=1)
    if granularity == "week":
        return day + timedelta(weeks=1)
    year, month = day.year, day.month + 1
    if month == 13:
        year, month = year + 1, 1
    return date(year, month, 1)


def _intervals(first, last, granularity, all_days):
    if first is None:
        days = [day for day in all_days if day <= last]
        if not days:
            return []
        first = min(days)
    cursor = _bucket(first, granularity)
    result = []
    while cursor <= last:
        nxt = _next_bucket(cursor, granularity)
        bucket_start, bucket_end = max(cursor, first), min(nxt - timedelta(days=1), last)
        result.append({"key": cursor.isoformat(), "start": bucket_start.isoformat(),
                       "end": bucket_end.isoformat(), "values": {key: 0 for key in METRICS},
                       "application_ids": {key: [] for key in METRICS}})
        cursor = nxt
    return result


def _app_rows(facts):
    rows, duplicates = {}, 0
    for app in facts.get("applications", []) if isinstance(facts.get("applications"), list) else []:
        if not isinstance(app, dict):
            continue
        app_id = app.get("application_id")
        if not isinstance(app_id, str) or not app_id or len(app_id) > 2048:
            continue
        if app_id in rows:
            duplicates += 1
        else:
            rows[app_id] = app
    return rows, duplicates


def _unique_dates(entries, value_key):
    dates = set()
    missing = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get(value_key)
        parsed = _local_day(value)
        if parsed is None:
            missing += 1
        else:
            dates.add(parsed)
    return dates, missing


def _coverage(source):
    source = source if isinstance(source, dict) else {}
    state = source.get("status", "unknown")
    return state in ("fresh", "stale") and source.get("coverage_status") != "partial"


def build_report(snapshot, period="last12weeks", *, start=None, end=None,
                 catalog=None):
    """Build versioned report data from ``projection.read_snapshot()`` output.

    ``period`` accepts last12weeks (default), thisweek, thismonth,
    last3months, alltime, or custom. Custom bounds are inclusive Singapore
    local dates. Output includes per-bin canonical application IDs for drilldown.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("facts"), dict):
        raise ValueError("a cached report snapshot is required")
    facts, sources = snapshot["facts"], snapshot.get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    first, last, granularity = _window(period, snapshot.get("as_of"), start, end)
    apps, duplicate_applications = _app_rows(facts)
    observations = defaultdict(dict)
    all_days, diagnostics = [], Counter()

    # Exact manifest confirmation is separate from tracker/queue claims.
    confirmed_by_app = defaultdict(list)
    for item in facts.get("confirmed_submissions", []) if isinstance(facts.get("confirmed_submissions"), list) else []:
        if isinstance(item, dict) and item.get("application_id") in apps:
            confirmed_by_app[item["application_id"]].append(item)
    confirmed_conflicts = set()
    for app_id, entries in confirmed_by_app.items():
        dates, missing = _unique_dates(entries, "submitted_at")
        if len(dates) == 1 and missing == 0:
            observations["confirmed_submissions"][app_id] = next(iter(dates))
            all_days.extend(dates)
        elif len(dates) > 1:
            diagnostics["conflicting_confirmed_dates"] += 1
            confirmed_conflicts.add(app_id)
        elif entries or missing:
            diagnostics["confirmed_time_unknown"] += 1

    claims_conflicts, claims_missing = set(), 0
    for app_id, app in apps.items():
        claims = app.get("submission_date_claims", [])
        if not isinstance(claims, list):
            continue
        dated = set()
        missing_for_app = False
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            if claim.get("invalid") or claim.get("missing") or not claim.get("value"):
                missing_for_app = True
                continue
            value = _local_day(claim.get("value"))
            if value is None:
                missing_for_app = True
            else:
                dated.add(value)
        if len(dated) > 1:
            claims_conflicts.add(app_id)
            diagnostics["conflicting_claim_dates"] += 1
        elif len(dated) == 1:
            observations["recorded_claims"][app_id] = next(iter(dated))
            all_days.extend(dated)
        elif claims or missing_for_app:
            claims_missing += 1
    diagnostics["claims_missing_or_invalid_time"] += claims_missing

    activity_coverage = facts.get("activity_coverage")
    activity_records = facts.get("activities", [])
    activity_coverage_ok = (isinstance(activity_coverage, dict)
                            and activity_coverage.get("complete") is True)
    activity_groups = defaultdict(dict)
    activity_ids = defaultdict(set)
    for record in activity_records if isinstance(activity_records, list) else []:
        if not isinstance(record, dict):
            diagnostics["invalid_activity_records"] += 1
            continue
        app_id, kind = record.get("application_id"), record.get("kind")
        if app_id not in apps or kind not in ACTIVITY_KINDS:
            diagnostics["invalid_activity_records"] += 1
            continue
        day = _local_day(record.get("completed_at"))
        if day is None:
            diagnostics["activity_time_unknown"] += 1
            continue
        raw_id = record.get("activity_id")
        stable_id = raw_id if isinstance(raw_id, str) and raw_id else "{}:{}:{}".format(app_id, kind, day)
        prior = activity_ids[(app_id, kind)].intersection({stable_id})
        if prior:
            prior_day = activity_groups[(app_id, kind)].get(stable_id)
            if prior_day != day:
                activity_groups[(app_id, kind)][stable_id] = None
                diagnostics["conflicting_activity_times"] += 1
            else:
                diagnostics["duplicate_activities"] += 1
            continue
        activity_ids[(app_id, kind)].add(stable_id)
        activity_groups[(app_id, kind)][stable_id] = day
    for (app_id, kind), entries in activity_groups.items():
        days = {value for value in entries.values() if value is not None}
        # Dated activity is unique by canonical application, kind, and local day.
        for day in days:
            observations[kind][(app_id, day)] = day
            all_days.append(day)

    if first is None:
        first = min(all_days) if all_days else None
    bins = _intervals(first, last, granularity, all_days)
    bin_by_start = {item["key"]: item for item in bins}
    for metric, entries in observations.items():
        for identity, day in entries.items():
            canonical_id = identity[0] if isinstance(identity, tuple) else identity
            if first is not None and first <= day <= last:
                bucket = _bucket(day, granularity).isoformat()
                target = bin_by_start.get(bucket)
                if target:
                    target["values"][metric] += 1
                    target["application_ids"][metric].append(canonical_id)

    # Build the application population from dated cached facts; alltime includes
    # every canonical application, including those with unknown dates.
    selected = set(apps) if period == "alltime" else set()
    unknown_application_time = 0
    for app_id in apps:
        dated = [day for mapping in observations.values()
                 for identity, day in mapping.items()
                 if (identity == app_id or (isinstance(identity, tuple) and identity[0] == app_id))
                 and isinstance(day, date)]
        if any(first <= day <= last for day in dated) if first is not None else bool(dated):
            selected.add(app_id)
        elif not dated:
            unknown_application_time += 1

    stage_counts = Counter()
    for app in apps.values():
        row = app.get("row") if isinstance(app.get("row"), dict) else {}
        stage = row.get("stage") or row.get("status") or "unknown"
        stage_counts[str(stage)] += 1

    catalog = catalog or {"schema_version": 1, "records": []}
    company = {"application_count": len(selected), "known_company_count": 0,
               "unresolved_application_count": 0,
               "size": Counter({"SME": 0, "large": 0, "unknown": 0}),
               "footprint": Counter({"domestic": 0, "multinational": 0, "unknown": 0}),
               "primary_industry": Counter({"unknown": 0}),
               "classification_coverage": {"classified": 0, "unknown": 0}}
    company_matches = {}
    unique_companies = {}
    for app_id in sorted(selected):
        app = apps[app_id]
        row = app.get("row") if isinstance(app.get("row"), dict) else {}
        record = company_catalog.resolve(catalog, row.get("company"))
        if record is None:
            company["unresolved_application_count"] += 1
            continue
        company_matches[app_id] = record["key"]
        unique_companies.setdefault(record["key"], record)
    company["known_company_count"] = len(unique_companies)
    for record in unique_companies.values():
        company["size"][record["size"]] += 1
        company["footprint"][record["footprint"]] += 1
        company["primary_industry"][record["primary_industry"]] += 1
        if (record["size"] != "unknown" or record["footprint"] != "unknown"
                or record["primary_industry"].casefold() != "unknown"):
            company["classification_coverage"]["classified"] += 1
        else:
            company["classification_coverage"]["unknown"] += 1
    company["size"] = dict(company["size"])
    company["footprint"] = dict(company["footprint"])
    company["primary_industry"] = dict(company["primary_industry"])

    claim_coverage = (_coverage(sources.get("tracker")) and _coverage(sources.get("queue"))
                      and not claims_conflicts and not claims_missing)
    confirmed_coverage = (_coverage(sources.get("snapshots"))
                          and "confirmed_submissions" in facts
                          and not confirmed_conflicts
                          and not diagnostics["confirmed_time_unknown"])
    activity_coverage_ok = (activity_coverage_ok
                            and not diagnostics["activity_time_unknown"]
                            and not diagnostics["conflicting_activity_times"]
                            and not diagnostics["invalid_activity_records"]
                            and not facts.get("activity_unmatched_count", 0))
    metrics_coverage = {
        "confirmed_submissions": "complete" if confirmed_coverage else "unknown_or_partial",
        "recorded_claims": "complete" if claim_coverage else "unknown_or_partial",
        "initial_tailoring": "complete" if activity_coverage_ok else "unknown_or_partial",
        "retailoring": "complete" if activity_coverage_ok else "unknown_or_partial",
    }
    for bucket in bins:
        for metric in METRICS:
            bucket["application_ids"][metric] = sorted(set(bucket["application_ids"][metric]))
    observed_totals = {metric: sum(item["values"][metric] for item in bins)
                       for metric in METRICS}
    for bucket in bins:
        bucket["observed_values"] = dict(bucket["values"])
        bucket["coverage"] = dict(metrics_coverage)
        for metric in METRICS:
            if metrics_coverage[metric] != "complete":
                bucket["values"][metric] = None
    return {"schema_version": 1, "timezone": TZ_NAME,
            "period": {"name": period or "last12weeks",
                       "start": first.isoformat() if first else None,
                       "end": last.isoformat(), "granularity": granularity},
            "metrics": metrics_coverage,
            "totals": {metric: (observed_totals[metric]
                                if metrics_coverage[metric] == "complete" else None)
                       for metric in METRICS},
            "observed_totals": observed_totals,
            "series": bins,
            "current_stage_counts": dict(sorted(stage_counts.items())),
            "applications": {"canonical_count": len(apps),
                             "selected_count": len(selected),
                             "unknown_time_count": unknown_application_time,
                             "duplicate_records": duplicate_applications,
                             "identity_conflict_count": len(facts.get("identity_conflicts", []))},
            "submission_timing": {"claim_conflicts": len(claims_conflicts),
                                  "confirmed_conflicts": len(confirmed_conflicts),
                                  "claims_missing_or_invalid": claims_missing},
            "activity_timing": {key: value for key, value in diagnostics.items()},
            "companies": company,
            "company_keys_by_application": company_matches}

