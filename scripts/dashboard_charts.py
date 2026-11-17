"""Cached analytics view data and exact chart-to-application selection."""
from collections import Counter, defaultdict

import application_explorer
import activity_analytics


def build_report(snapshot, period="last12weeks", start=None, end=None):
    """Build analytics only from the already cached sanitized snapshot."""
    return activity_analytics.build_report(snapshot, period, start=start, end=end)


def _ids_in_bins(report, metric, start, end):
    selected = set()
    for item in report.get("series", []):
        if item.get("start", "") >= start and item.get("end", "") <= end:
            selected.update(item.get("application_ids", {}).get(metric, []))
    return selected


def metric_application_ids(report, metric, start, end):
    """Return canonical IDs from exactly the requested metric and clipped dates."""
    if metric not in activity_analytics.METRICS:
        raise ValueError("unsupported analytics metric")
    if not isinstance(start, str) or not isinstance(end, str) or start > end:
        raise ValueError("invalid analytics selection dates")
    return sorted(_ids_in_bins(report, metric, start, end))


def population_application_ids(snapshot, report):
    """Return the canonical application population used for company views."""
    if report.get("period", {}).get("name") == "alltime":
        applications = snapshot.get("facts", {}).get("applications", [])
        return sorted({row.get("application_id") for row in applications
                       if isinstance(row, dict) and isinstance(row.get("application_id"), str)})
    selected = set()
    for item in report.get("series", []):
        for ids in item.get("application_ids", {}).values():
            selected.update(ids)
    return sorted(selected)


def _company_views(snapshot, report):
    facts = snapshot.get("facts", {})
    facts = facts if isinstance(facts, dict) else {}
    catalog = facts.get("company_catalog", {})
    records = {row.get("key"): row for row in catalog.get("records", [])
               if isinstance(row, dict) and isinstance(row.get("key"), str)}
    app_keys = report.get("company_keys_by_application", {})
    app_ids = population_application_ids(snapshot, report)
    app_counts = Counter()
    app_to_key = {}
    for app_id in app_ids:
        key = app_keys.get(app_id)
        if key in records:
            app_counts[key] += 1
            app_to_key[app_id] = key

    dimensions = {
        "size": lambda record: record.get("size", "unknown"),
        "footprint": lambda record: record.get("footprint", "unknown"),
        "industry": lambda record: str(record.get("primary_industry") or "unknown").strip(),
    }
    views = {}
    for dimension, get_value in dimensions.items():
        companies, applications = defaultdict(set), Counter()
        for key in app_counts:
            record = records[key]
            value = get_value(record) or "unknown"
            if value.casefold() == "unknown":
                value = "Unknown"
            companies[value].add(key)
        for key, count in app_counts.items():
            value = get_value(records[key]) or "unknown"
            if value.casefold() == "unknown":
                value = "Unknown"
            applications[value] += count
        labels = set(companies) | set(applications)
        views[dimension] = [{"label": label, "companies": len(companies[label]),
                             "applications": applications[label]}
                            for label in sorted(labels, key=lambda name: (-applications[name], name.casefold()))]

    company_rows = []
    for key, count in app_counts.items():
        record = records[key]
        aliases = record.get("aliases", [])
        label = aliases[0] if aliases else key
        company_rows.append({"key": key, "label": label, "applications": count})
    company_rows.sort(key=lambda row: (-row["applications"], row["label"].casefold(), row["key"]))
    unresolved = max(0, len(app_ids) - len(app_to_key))
    return {"dimensions": views, "companies": company_rows,
            "unresolved_applications": unresolved,
            "application_ids_by_company": {
                key: [app_id for app_id, company_key in app_to_key.items() if company_key == key]
                for key in app_counts}}


def build_view(snapshot, period="last12weeks", start=None, end=None):
    report = build_report(snapshot, period, start, end)
    monthly = build_report(snapshot, "alltime")
    monthly["series"] = monthly.get("series", [])[-12:]
    return {"report": report, "monthly": monthly,
            "companies": _company_views(snapshot, report)}


def filter_applications(snapshot, rows, state):
    """Apply metric/bin and company selection before ordinary explorer filters."""
    metric, company = state.get("metric"), state.get("company")
    if not metric and not company:
        return rows, None
    if metric:
        report = build_report(snapshot, "custom", state["from"], state["to"])
        selected = set(metric_application_ids(report, metric, state["from"], state["to"]))
        observations = sum(item.get("observed_values", {}).get(metric, 0)
                           for item in report.get("series", [])
                           if item.get("start", "") >= state["from"]
                           and item.get("end", "") <= state["to"])
        count_label = "recorded activity days" if metric in ("initial_tailoring", "retailoring") else "applications"
        note = "{}: {} observed across {} canonical applications (Asia/Singapore dates {} to {}).".format(
            metric.replace("_", " ").capitalize(), observations, len(selected),
            state["from"], state["to"])
    else:
        report = build_report(snapshot, state.get("period", "last12weeks"),
                              state.get("start"), state.get("end"))
        selected = set(population_application_ids(snapshot, report))
        note = "Selected reporting population: {} canonical applications.".format(len(selected))
    if company:
        selected = {app_id for app_id in selected
                    if report.get("company_keys_by_application", {}).get(app_id) == company}
        note += " Company key {}: {} matching applications.".format(company, len(selected))
    opaque = {application_explorer.opaque_key(app_id) for app_id in selected}
    result = [row for row in rows if row.get("application_key") in opaque]
    return result, note

