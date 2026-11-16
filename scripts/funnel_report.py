#!/usr/bin/env python3
"""Deterministic, evidence-based hiring funnel report (stdlib only)."""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfoNotFoundError

from config import default_repo_dir, default_runtime_path, path_setting
import report_snapshot


NON_OUTCOMES = {"unrelated", "unclassified", "account_admin", "job_alert", "acknowledgement"}
CALLBACK_TYPES = {"recruiter_reply", "availability_request"}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _submission_date(application, zone):
    dates, exclusions = set(), {"invalid": 0, "missing": 0}
    for claim in application.get("submission_date_claims", []):
        if claim.get("invalid"):
            exclusions["invalid"] += 1
            continue
        if claim.get("missing"):
            exclusions["missing"] += 1
            continue
        value = claim.get("value")
        if not value:
            continue
        if claim.get("source") == "queue" and "T" in value:
            stamp = _timestamp(value)
            if stamp:
                dates.add(stamp.astimezone(zone).date())
            else:
                exclusions["invalid"] += 1
        else:
            try:
                dates.add(date.fromisoformat(value))
            except (TypeError, ValueError):
                exclusions["invalid"] += 1
    moments = []
    for value in application.get("submission_moments", []):
        stamp = _timestamp(value)
        if stamp:
            moments.append(stamp)
    return dates, exclusions, moments


def _outcome_category(event_type, origin):
    if not isinstance(event_type, str):
        return None
    if event_type in CALLBACK_TYPES:
        return "callback" if origin == "human" else None
    if event_type.startswith("oa_") or event_type in {"assessment", "online_assessment"}:
        return "oa"
    if event_type.startswith("interview_"):
        return "interview"
    if event_type in {"offer", "offer_received", "offer_accepted"}:
        return "offer"
    if event_type in NON_OUTCOMES:
        return None
    return "other"


def _stats(values):
    if not values:
        return {"n": 0, "min_days": None, "median_days": None, "max_days": None}
    ordered = sorted(values)
    return {"n": len(ordered), "min_days": round(ordered[0], 3),
            "median_days": round(median(ordered), 3),
            "max_days": round(ordered[-1], 3)}


def _rate(count, denominator):
    return round(count / denominator, 6) if denominator else None


def _cohort(snapshot, *, days, as_of, zone, applications_available,
            events_available, event_source_status):
    facts = snapshot.get("facts", {})
    apps = facts.get("applications", [])
    all_application_ids = {app.get("application_id") for app in apps}
    alias_map = facts.get("application_aliases", {})
    cutoff = as_of - timedelta(days=days)
    exclusions = {"identity_conflicts": len(facts.get("identity_conflicts", [])),
                  "unknown_submission_date": 0, "invalid_submission_date": 0,
                  "conflicting_submission_date": 0, "future_submission": 0,
                  "not_mature": 0, "missing_resume_provenance": 0,
                  "unmatched_events": 0, "ambiguous_event_identity": 0,
                  "unverified_events": 0, "unknown_human_origin": 0,
                  "automated_callback_origin": 0,
                  "invalid_event_id": 0, "unknown_event_type": 0,
                  "invalid_event_date": 0, "events_after_as_of": 0,
                  "events_for_ineligible_applications": 0,
                  "events_before_or_on_submission": 0, "events_outside_window": 0,
                  "quarantined_event_ids": 0}
    eligible = []
    for app in apps:
        dates, date_exclusions, moments = _submission_date(app, zone)
        exclusions["invalid_submission_date"] += date_exclusions["invalid"]
        if len(dates) > 1:
            exclusions["conflicting_submission_date"] += 1
            continue
        if not dates:
            if app.get("submission_date_claims"):
                exclusions["unknown_submission_date"] += 1
            continue
        submitted = next(iter(dates))
        if submitted > as_of:
            exclusions["future_submission"] += 1
            continue
        if submitted > cutoff:
            exclusions["not_mature"] += 1
            continue
        if not app.get("resume_hash"):
            exclusions["missing_resume_provenance"] += 1
        submitted_at = min(moments) if moments else None
        eligible.append((app, submitted, submitted_at))

    eligible_by_id = {app["application_id"]: (app, submitted, submitted_at)
                      for app, submitted, submitted_at in eligible}
    outcomes = {"callback": set(), "oa": set(), "interview": set(), "offer": set(), "other": set()}
    event_type_apps = defaultdict(set)
    first_response = {}
    conflicted_aliases = set(facts.get("conflicted_application_aliases", []))
    quarantined_seen = set()
    if events_available:
        for event in facts.get("events", []):
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                exclusions["invalid_event_id"] += 1
                continue
            if event.get("quarantined"):
                if event_id not in quarantined_seen:
                    exclusions["quarantined_event_ids"] += 1
                    quarantined_seen.add(event_id)
                continue
            if event.get("verification") != "verified":
                exclusions["unverified_events"] += 1
                continue
            kind = event.get("event_type")
            origin = event.get("origin")
            if not isinstance(kind, str) or not kind:
                exclusions["unknown_event_type"] += 1
                continue
            if kind in CALLBACK_TYPES and origin != "human":
                if origin == "automated":
                    exclusions["automated_callback_origin"] += 1
                else:
                    exclusions["unknown_human_origin"] += 1
                continue
            application_id = event.get("application_id")
            if not isinstance(application_id, str) or not application_id:
                exclusions["unmatched_events"] += 1
                continue
            if application_id in conflicted_aliases:
                exclusions["ambiguous_event_identity"] += 1
                continue
            canonical = alias_map.get(application_id, application_id)
            if canonical not in eligible_by_id:
                if canonical in all_application_ids:
                    exclusions["events_for_ineligible_applications"] += 1
                else:
                    exclusions["unmatched_events"] += 1
                continue
            occurred = _timestamp(event.get("occurred_at"))
            if not occurred:
                exclusions["invalid_event_date"] += 1
                continue
            local_occurred = occurred.astimezone(zone)
            day = local_occurred.date()
            if day > as_of:
                exclusions["events_after_as_of"] += 1
                continue
            app, submitted, submitted_at = eligible_by_id[canonical]
            if day < submitted:
                exclusions["events_before_or_on_submission"] += 1
                continue
            if day == submitted and (not submitted_at or occurred <= submitted_at):
                exclusions["events_before_or_on_submission"] += 1
                continue
            if day > submitted + timedelta(days=days):
                exclusions["events_outside_window"] += 1
                continue
            category = _outcome_category(kind, origin)
            if not category:
                continue
            outcomes[category].add(canonical)
            event_type_apps[kind].add(canonical)
            elapsed = float((day - submitted).days)
            key = (category, canonical)
            first_response[key] = min(elapsed, first_response.get(key, elapsed))

    response_days = defaultdict(list)
    for (category, _canonical), elapsed in first_response.items():
        response_days[category].append(elapsed)

    groups = {"source": defaultdict(list), "role": defaultdict(list),
              "resume_hash": defaultdict(list)}
    for app, submitted, _submitted_at in eligible:
        row = app.get("row", {})
        values = {"source": row.get("source") or "unknown",
                  "role": row.get("role") or "unknown",
                  "resume_hash": app.get("resume_hash") or "unknown"}
        for dimension, value in values.items():
            groups[dimension][value].append(app["application_id"])

    grouped_rates = {}
    for dimension, members in groups.items():
        rows = []
        for value, ids in sorted(members.items()):
            denominator = len(ids)
            observed = {name: len(ids_set.intersection(ids)) if events_available else None
                        for name, ids_set in outcomes.items()}
            rows.append({"group": value, "denominator": denominator,
                         "observed": observed,
                         "rates": {name: _rate(count, denominator) if count is not None else None
                                   for name, count in observed.items()}})
        grouped_rates[dimension] = rows

    if not applications_available:
        for key in exclusions:
            exclusions[key] = None
        grouped_rates = {dimension: None for dimension in groups}
    elif not events_available:
        for key in ("unmatched_events", "ambiguous_event_identity", "unverified_events",
                    "unknown_human_origin", "automated_callback_origin",
                    "invalid_event_id", "unknown_event_type", "invalid_event_date",
                    "events_after_as_of", "events_for_ineligible_applications",
                    "events_before_or_on_submission", "events_outside_window",
                    "quarantined_event_ids"):
            exclusions[key] = None
    denominator = len(eligible) if applications_available else None
    outcome_is_observable = applications_available and events_available
    ledger_coverage = facts.get("event_coverage", {})
    coverage_flag = ledger_coverage.get("complete")
    coverage = ({"status": "complete" if coverage_flag is True else
                           "partial" if coverage_flag is False else "unknown",
                 "ledger": ledger_coverage} if events_available else None)

    return {
        "window_days": days,
        "as_of": as_of.isoformat(),
        "cutoff_date": cutoff.isoformat(),
        "cutoff_rule": "submission date on or before cutoff is eligible; event dates are strictly after submission and on or before submission + window",
        "eligible_denominator": denominator,
        "eligible_applications": denominator,
        "exclusions": exclusions,
        "observed_numerator": {name: len(ids) if outcome_is_observable else None
                               for name, ids in outcomes.items()},
        "outcome_rates": {name: _rate(len(ids), denominator) if outcome_is_observable else None
                          for name, ids in outcomes.items()},
        "event_type_observed_applications": ({kind: len(ids)
                                              for kind, ids in sorted(event_type_apps.items())}
                                             if outcome_is_observable else None),
        "response_timing_days": {name: _stats(response_days[name])
                                 if outcome_is_observable else None
                                 for name in ("callback", "oa", "interview", "offer", "other")},
        "grouped_observed_rates": grouped_rates,
        "event_source_status": event_source_status,
        "coverage": coverage,
        "coverage_caveat": "Observed verified events only. A mature application without an event is absent evidence, not a confirmed non-response.",
    }


def build_report(snapshot, *, as_of=None, timezone_name="Asia/Singapore"):
    """Pure report construction from normalized snapshot facts."""
    zone = report_snapshot.timezone_for(timezone_name)
    as_of = date.fromisoformat(as_of or snapshot["as_of"])
    sources = snapshot.get("sources", {})
    event_source = sources.get("events", {})
    applications_available = any(
        isinstance(sources.get(name), dict)
        and sources[name].get("status") in ("fresh", "stale")
        for name in ("tracker", "queue"))
    events_available = event_source.get("status") in ("fresh", "stale")
    return {
        "schema_version": 1,
        "snapshot_id": snapshot.get("snapshot_id"),
        "as_of": as_of.isoformat(),
        "timezone": timezone_name,
        "maturity_and_cutoff": "as_of is an inclusive local calendar day through its end. Maturity cutoff is inclusive: applied date <= as_of - window days. Within each cohort, event date must be strictly after applied date and inclusive through applied date + window days, and not after as_of.",
        "mature_cohorts": [
            _cohort(snapshot, days=days, as_of=as_of, zone=zone,
                    applications_available=applications_available,
                    events_available=events_available,
                    event_source_status=event_source.get("status", "unavailable"))
            for days in (14, 28)
        ],
        "identity_diagnostics": facts_diagnostics(snapshot),
        "sources": sources,
        "grouping_caveat": "Source, role and submitted resume hash groups are descriptive observed rates. They do not show causation. Current resume bytes are never substituted for submitted provenance.",
        "human_callback_rule": "Callback counts include only verified events with separately verified origin=human.",
        "response_timing_basis": "Calendar days from submission date to event date; no submission clock time is inferred.",
    }


def facts_diagnostics(snapshot):
    facts = snapshot.get("facts", {})
    sources = snapshot.get("sources", {})
    apps_available = any(isinstance(sources.get(name), dict)
                         and sources[name].get("status") in ("fresh", "stale")
                         for name in ("tracker", "queue"))
    events_available = (isinstance(sources.get("events"), dict)
                        and sources["events"].get("status") in ("fresh", "stale"))
    return {"identity_conflict_count": len(facts.get("identity_conflicts", []))
            if apps_available else None,
            "identity_conflict_reasons": dict(sorted(
                (key, sum(1 for item in facts.get("identity_conflicts", [])
                          if item.get("reason") == key))
                for key in {item.get("reason", "unknown") for item in facts.get("identity_conflicts", [])}))
            if apps_available else None,
            "effective_event_diagnostics": facts.get("event_diagnostics", {})
            if events_available else None,
            "quarantined_event_count": facts.get("quarantined_event_count", 0)
            if events_available else None}


def render_text(report):
    lines = ["Application funnel observations through {} ({})".format(
        report["as_of"], report["timezone"])]
    for cohort in report["mature_cohorts"]:
        lines.append("\n{}-day cohort: {} eligible applications (submission date <= {})".format(
            cohort["window_days"], cohort["eligible_denominator"], cohort["cutoff_date"]))
        for outcome, count in cohort["observed_numerator"].items():
            lines.append("  {}: {} / {}".format(
                outcome, count if count is not None else "unavailable",
                cohort["eligible_denominator"]))
        lines.append("  Event source: {}".format(cohort["event_source_status"]))
        lines.append("  Exclusions: " + json.dumps(cohort["exclusions"], sort_keys=True))
        lines.append("  Event types: " + json.dumps(
            cohort["event_type_observed_applications"], sort_keys=True))
        lines.append("  Response timing: " + json.dumps(
            cohort["response_timing_days"], sort_keys=True))
        lines.append("  Coverage: " + json.dumps(cohort["coverage"], sort_keys=True))
        lines.append("  Group denominators: " + json.dumps(cohort["grouped_observed_rates"],
                                                                  sort_keys=True, ensure_ascii=False))
        lines.append("  " + cohort["coverage_caveat"])
    lines.append(report["grouping_caveat"])
    lines.append(report["human_callback_rule"])
    lines.append(report["maturity_and_cutoff"])
    lines.append("Response timing: " + report["response_timing_basis"])
    lines.append("Identity and evidence diagnostics: " + json.dumps(
        report["identity_diagnostics"], sort_keys=True))
    lines.append("Sources: " + ", ".join("{}={}{}".format(
        name, value["status"], "(" + value["error_code"] + ")" if value.get("error_code") else "")
                                           for name, value in sorted(report["sources"].items())))
    return "\n".join(lines)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=default_repo_dir())
    parser.add_argument("--tracker", default=None)
    parser.add_argument("--queue", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--ingest-state", default=None)
    parser.add_argument("--snapshot-root", default=None)
    parser.add_argument("--as-of", default=None, help="fixed local date, YYYY-MM-DD")
    parser.add_argument("--timezone", default=os.environ.get("JOBSEARCH_TIMEZONE", "Asia/Singapore"))
    parser.add_argument("--json", action="store_true")
    return parser


def collect(args):
    zone = report_snapshot.timezone_for(args.timezone)
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(zone).date()
    return report_snapshot.collect_snapshot(
        tracker_path=args.tracker or path_setting(
            "JOBSEARCH_TRACKER", os.path.join(args.repo, "applications.yaml")),
        queue_path=args.queue or path_setting("JOBSEARCH_QUEUE", default_runtime_path("queue.yaml")),
        ledger_path=args.ledger or path_setting("JOBSEARCH_RECRUITMENT_LEDGER",
                                                 default_runtime_path("recruitment_events.json")),
        ingestion_path=args.ingest_state or path_setting("JOBSEARCH_INGEST_STATE",
                                                          default_runtime_path("email_ingest_state.json")),
        snapshot_root=args.snapshot_root or path_setting("JOBSEARCH_SNAPSHOT_ROOT",
                                                          default_runtime_path("submission-snapshots")),
        as_of=as_of.isoformat())


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        snapshot = collect(args)
        report = build_report(snapshot, as_of=args.as_of, timezone_name=args.timezone)
    except (OSError, ValueError, ZoneInfoNotFoundError) as exc:
        print("funnel_report: invalid report input: {}".format(type(exc).__name__), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
