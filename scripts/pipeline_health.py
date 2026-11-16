#!/usr/bin/env python3
"""Deterministic daily submission and pipeline health report (stdlib only)."""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfoNotFoundError

from config import default_repo_dir, default_runtime_path, path_setting
import report_snapshot


STALE_CLAIM_SECONDS = 4 * 60 * 60
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _local_datetime(value, zone):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(zone)


def _submission_day(application, zone):
    claims = application.get("submission_date_claims", [])
    dates, invalid = set(), 0
    for claim in claims:
        if claim.get("invalid"):
            invalid += 1
            continue
        value = claim.get("value")
        if not value:
            continue
        if claim.get("source") == "queue" and "T" in value:
            stamp = _local_datetime(value, zone)
            if stamp:
                dates.add(stamp.date())
            else:
                invalid += 1
        else:
            try:
                dates.add(date.fromisoformat(value))
            except (TypeError, ValueError):
                invalid += 1
    return dates, invalid


def _source_usable(source):
    return isinstance(source, dict) and source.get("status") in ("fresh", "stale")


def build_report(snapshot, *, timezone_name, weekday_goal, weekend_goal,
                 ready_buffer, as_of=None, stale_after_seconds=STALE_CLAIM_SECONDS):
    """Build a daily view from one normalized snapshot; performs no source reads."""
    zone = report_snapshot.timezone_for(timezone_name)
    as_of = date.fromisoformat(as_of or snapshot["as_of"])
    sources = snapshot.get("sources", {})
    facts = snapshot.get("facts", {})
    tracker_ok = _source_usable(sources.get("tracker"))
    queue_ok = _source_usable(sources.get("queue"))
    applications = facts.get("applications", [])
    app_sources_available = tracker_ok or queue_ok
    submission_count = 0
    date_exclusions = 0
    identity_exclusions = len(facts.get("identity_conflicts", []))
    for application in applications:
        dates, invalid = _submission_day(application, zone)
        date_exclusions += invalid + sum(1 for claim in application.get("submission_date_claims", [])
                                         if claim.get("missing"))
        if len(dates) > 1:
            date_exclusions += 1
            continue
        if dates and next(iter(dates)) == as_of:
            submission_count += 1
    goal = weekday_goal if as_of.weekday() < 5 else weekend_goal
    queue_rows = facts.get("attempts", [row for app in applications
                                         for row in app.get("queue_rows", [])])
    ready_count = sum(1 for row in queue_rows if row.get("status") == "queued")
    in_progress = sum(1 for row in queue_rows if row.get("status") == "in_progress")
    outcome_unknown = sum(1 for row in queue_rows if row.get("status") == "outcome_unknown")
    stale = 0
    snapshot_gaps = 0
    snapshot_unverifiable = 0
    pending_sync = 0
    sync_unknown = 0
    blockers = {}
    unclassified_blockers = 0
    reference = _local_datetime(snapshot.get("generated_at"), zone)
    for row in queue_rows:
        if row.get("status") == "in_progress":
            claimed = _local_datetime(row.get("claimed_at"), zone)
            if reference and claimed and (reference - claimed).total_seconds() > stale_after_seconds:
                stale += 1
        if row.get("status") == "submitted":
            if (row.get("snapshot_provenance") != "complete"
                    or not row.get("snapshot_id")
                    or row.get("snapshot_manifest_status") == "missing_or_invalid"):
                snapshot_gaps += 1
            elif row.get("snapshot_manifest_status") == "unavailable":
                snapshot_unverifiable += 1
            sync = row.get("tracker_sync")
            if sync in ("pending", "error", "syncing"):
                pending_sync += 1
            elif sync != "synced":
                sync_unknown += 1
        code = row.get("blocker_code")
        if code:
            if CODE_RE.fullmatch(str(code)):
                blockers[code] = blockers.get(code, 0) + 1
            else:
                unclassified_blockers += 1

    ingestion_source = sources.get("ingestion", {})
    ingestion = facts.get("ingestion", {})
    last_scan = _local_datetime(ingestion.get("last_scan_at"), zone)
    ingestion_age = (max(0, int((reference - last_scan).total_seconds()))
                     if reference and last_scan else None)
    scan_status = ("unknown" if ingestion_age is None else
                   "stale" if ingestion_age > ingestion_source.get("freshness_seconds", 86400)
                   else "fresh")
    submissions = {
        "count": (submission_count if tracker_ok else
                  submission_count if queue_ok and submission_count else None),
        "coverage": ("stale" if any(sources.get(name, {}).get("status") == "stale"
                                    for name in ("tracker", "queue")) else
                     "complete" if tracker_ok and queue_ok else
                     "partial" if tracker_ok or queue_ok else "unavailable"),
        "eligible_records": len(applications) if app_sources_available else None,
        "excluded_identity_conflicts": identity_exclusions if app_sources_available else None,
        "excluded_date_claims": date_exclusions if app_sources_available else None,
    }
    report = {
        "schema_version": 1,
        "snapshot_id": snapshot.get("snapshot_id"),
        "as_of": as_of.isoformat(),
        "timezone": timezone_name,
        "local_weekday": as_of.strftime("%A"),
        "submission_goal": {"target": goal, "confirmed": submissions["count"],
                            "remaining": max(0, goal - submissions["count"])
                            if submissions["count"] is not None else None,
                            "submissions": submissions},
        "ready_buffer": {"target": ready_buffer,
                          "queued": ready_count if queue_ok else None,
                          "gap": max(0, ready_buffer - ready_count) if queue_ok else None},
        "attempts": {"stale_in_progress": stale if queue_ok else None,
                     "in_progress": in_progress if queue_ok else None,
                     "outcome_unknown": outcome_unknown if queue_ok else None},
        "tracker_sync": {"pending_or_error": pending_sync if queue_ok else None,
                         "unknown_state": sync_unknown if queue_ok else None},
        "snapshot_gaps": {"known": snapshot_gaps if queue_ok else None,
                          "unverifiable_claims": snapshot_unverifiable if queue_ok else None,
                          "total": snapshot_gaps if queue_ok and not snapshot_unverifiable else None,
                          "assessment_complete": bool(queue_ok and _source_usable(sources.get("snapshots"))
                                                       and not snapshot_unverifiable),
                          "source_status": sources.get("snapshots", {}).get("status", "unavailable")},
        "blockers": {"by_code": dict(sorted(blockers.items())) if queue_ok else None,
                     "unclassified": unclassified_blockers if queue_ok else None},
        "ingestion": {"status": scan_status,
                      "state_source_status": ingestion_source.get("status", "unavailable"),
                      "error_code": ingestion_source.get("error_code"),
                      "last_scan_at": ingestion.get("last_scan_at"),
                      "age_seconds": ingestion_age,
                      "scope_count": ingestion.get("scope_count")},
        "sources": sources,
        "caveats": [
            "Unavailable sources have null dependent counts; a valid empty source is observed separately.",
            "Only structured blocker codes are counted. Unclassified blockers are not derived from notes.",
            "A missing reply is absent evidence and does not establish that no response occurred.",
        ],
    }
    return report


def render_text(report):
    goal = report["submission_goal"]
    submissions = goal["submissions"]
    lines = ["Daily pipeline health · {} ({})".format(report["as_of"], report["timezone"]),
             "Confirmed submissions: {} / {} ({} coverage)".format(
                 goal["confirmed"] if goal["confirmed"] is not None else "unavailable",
                 goal["target"], submissions["coverage"]),
             "Ready buffer: {} / {}".format(
                 report["ready_buffer"]["queued"] if report["ready_buffer"]["queued"] is not None else "unavailable",
                 report["ready_buffer"]["target"]),
             "Attempts: stale {}, in progress {}, outcome unknown {}".format(
                 report["attempts"]["stale_in_progress"], report["attempts"]["in_progress"],
                 report["attempts"]["outcome_unknown"]),
             "Tracker sync pending/error: {}; snapshot gaps: {}".format(
                 report["tracker_sync"]["pending_or_error"], report["snapshot_gaps"]),
             "Ingestion: {} (last scan {}, age {} seconds)".format(
                 report["ingestion"]["status"], report["ingestion"]["last_scan_at"],
                 report["ingestion"]["age_seconds"])]
    lines.append("Sources: " + ", ".join("{}={}{}".format(
        name, value["status"], "(" + value["error_code"] + ")" if value.get("error_code") else "")
                                           for name, value in sorted(report["sources"].items())))
    lines.extend(report["caveats"])
    return "\n".join(lines)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None,
                        help="cached projection database (read-only; collect separately)")
    parser.add_argument("--as-of", default=None, help="local calendar day, YYYY-MM-DD")
    parser.add_argument("--timezone", default=os.environ.get("JOBSEARCH_TIMEZONE", "Asia/Singapore"))
    parser.add_argument("--weekday-goal", type=int, default=1)
    parser.add_argument("--weekend-goal", type=int, default=0)
    parser.add_argument("--ready-buffer", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    return parser


def collect(args):
    zone = report_snapshot.timezone_for(args.timezone)
    as_of_value = getattr(args, "as_of", None)
    repo = getattr(args, "repo", default_repo_dir())
    as_of = date.fromisoformat(as_of_value) if as_of_value else datetime.now(zone).date()
    return report_snapshot.collect_snapshot(
        tracker_path=getattr(args, "tracker", None) or path_setting(
            "JOBSEARCH_TRACKER", os.path.join(repo, "applications.yaml")),
        queue_path=getattr(args, "queue", None) or path_setting(
            "JOBSEARCH_QUEUE", default_runtime_path("queue.yaml")),
        ledger_path=getattr(args, "ledger", None) or path_setting(
            "JOBSEARCH_RECRUITMENT_LEDGER", default_runtime_path("recruitment_events.json")),
        ingestion_path=getattr(args, "ingest_state", None) or path_setting(
            "JOBSEARCH_INGEST_STATE", default_runtime_path("email_ingest_state.json")),
        snapshot_root=getattr(args, "snapshot_root", None) or path_setting(
            "JOBSEARCH_SNAPSHOT_ROOT", default_runtime_path("submission-snapshots")),
        as_of=as_of.isoformat())


def main(argv=None):
    args = _parser().parse_args(argv)
    import projection
    if min(args.weekday_goal, args.weekend_goal, args.ready_buffer) < 0:
        print("pipeline_health: goals and ready buffer must be non-negative", file=sys.stderr)
        return 2
    try:
        snapshot = projection.read_snapshot(args.db)
        report = build_report(snapshot, timezone_name=args.timezone,
                              weekday_goal=args.weekday_goal,
                              weekend_goal=args.weekend_goal,
                              ready_buffer=args.ready_buffer, as_of=args.as_of)
    except projection.ProjectionError as exc:
        print("pipeline_health: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, ZoneInfoNotFoundError) as exc:
        print("pipeline_health: invalid report input: {}".format(type(exc).__name__), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
