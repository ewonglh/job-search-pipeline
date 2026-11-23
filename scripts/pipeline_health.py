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


def _age_summary(rows, timestamp_fields, reference, zone):
    ages = []
    unknown = 0
    for row in rows:
        stamp = None
        for field in timestamp_fields:
            stamp = _local_datetime(row.get(field), zone)
            if stamp is not None:
                break
        if reference is None or stamp is None:
            unknown += 1
        else:
            ages.append(max(0, int((reference - stamp).total_seconds())))
    return {"oldest_age_seconds": max(ages) if ages else None,
            "age_unknown_count": unknown}


def _intake_lane(intake, lane, reference, zone):
    lane_data = intake.get(lane, {}) if isinstance(intake, dict) else {}
    latest = lane_data.get("latest") if isinstance(lane_data, dict) else None
    if not isinstance(latest, dict):
        return {"status": "unknown", "run_count": lane_data.get("run_count", 0)
                if isinstance(lane_data, dict) else 0, "latest": None,
                "age_seconds": None, "recent": lane_data.get("recent", [])
                if isinstance(lane_data, dict) else []}
    recorded = _local_datetime(latest.get("recorded_at"), zone)
    age = max(0, int((reference - recorded).total_seconds())) if reference and recorded else None
    outcome = latest.get("outcome")
    if outcome not in {"healthy", "healthy_empty", "partial_failure", "total_failure", "failed"}:
        outcome = "unknown"
    return {"status": outcome,
            "run_count": lane_data.get("run_count", 1), "latest": latest,
            "age_seconds": age,
            "recent": lane_data.get("recent", []) if isinstance(lane_data, dict) else []}


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
    ready_rows = [row for row in queue_rows if row.get("status") == "queued"]
    blocked_rows = [row for row in queue_rows
                    if row.get("status") in ("blocked", "failed")
                    or (row.get("status") in ("queued", "in_progress")
                        and row.get("blocker_code")
                        and CODE_RE.fullmatch(str(row.get("blocker_code"))))]
    unknown_rows = [row for row in queue_rows
                    if row.get("status") == "outcome_unknown"
                    or row.get("attempt_state") == "outcome_unknown"]
    snapshot_backlog_rows = []
    sync_backlog_rows = []
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
                snapshot_backlog_rows.append(row)
            elif row.get("snapshot_manifest_status") == "unavailable":
                snapshot_unverifiable += 1
            sync = row.get("tracker_sync")
            if sync in ("pending", "error", "syncing"):
                pending_sync += 1
                sync_backlog_rows.append(row)
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
    queue_usable = bool(queue_ok)
    ready_ages = _age_summary(ready_rows, ("enqueued_at", "approved_at"), reference, zone)
    blocked_ages = _age_summary(blocked_rows,
                                ("blocked_at", "failed_at", "updated_at", "claimed_at"),
                                reference, zone)
    unknown_ages = _age_summary(unknown_rows,
                                ("outcome_unknown_at", "claimed_at"), reference, zone)
    snapshot_ages = _age_summary(snapshot_backlog_rows,
                                 ("snapshot_prepared_at", "submitted_at"), reference, zone)
    sync_ages = _age_summary(sync_backlog_rows,
                             ("tracker_sync_pending_at", "tracker_sync_started_at",
                              "tracker_sync_failed_at", "submitted_at"),
                             reference, zone)
    active_attempts = [row for row in queue_rows if row.get("status") == "in_progress"]
    browser_owners, browser_released, browser_unknown = [], 0, 0
    for row in active_attempts:
        released_at = _local_datetime(
            row.get("browser_owner_released_at") or row.get("browser_released_at"), zone)
        runner_id, session_ref = row.get("runner_id"), row.get("session_ref")
        if released_at:
            browser_released += 1
        elif runner_id or session_ref:
            claimed_at = _local_datetime(row.get("claimed_at"), zone)
            browser_owners.append({
                "runner_id": runner_id, "session_ref": session_ref,
                "attempt_age_seconds": (max(0, int((reference - claimed_at).total_seconds()))
                                        if reference and claimed_at else None),
            })
        else:
            browser_unknown += 1
    intake_source = sources.get("intake", {})
    intake_facts = facts.get("intake", {})
    intake = {"source_status": intake_source.get("status", "unavailable"),
              "coverage_status": intake_source.get("coverage_status", "unknown"),
              "coverage_reasons": intake_source.get("coverage_reasons", []),
              "error_code": intake_source.get("error_code"),
              "discovery": _intake_lane(intake_facts, "discovery", reference, zone),
              "enqueue": _intake_lane(intake_facts, "enqueue", reference, zone)}
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
                          "gap": max(0, ready_buffer - ready_count) if queue_ok else None,
                          "oldest_age_seconds": ready_ages["oldest_age_seconds"] if queue_usable else None,
                          "age_unknown_count": ready_ages["age_unknown_count"] if queue_usable else None},
        "attempts": {"stale_in_progress": stale if queue_ok else None,
                     "in_progress": in_progress if queue_ok else None,
                     "outcome_unknown": outcome_unknown if queue_ok else None,
                     "blocked": len(blocked_rows) if queue_usable else None,
                     "oldest_blocked_age_seconds": blocked_ages["oldest_age_seconds"] if queue_usable else None,
                     "blocked_age_unknown_count": blocked_ages["age_unknown_count"] if queue_usable else None,
                     "oldest_unknown_age_seconds": unknown_ages["oldest_age_seconds"] if queue_usable else None,
                     "unknown_age_unknown_count": unknown_ages["age_unknown_count"] if queue_usable else None},
        "browser_ownership": {"active": len(browser_owners) if queue_usable else None,
                              "released": browser_released if queue_usable else None,
                              "unknown": browser_unknown if queue_usable else None,
                              "owners": browser_owners if queue_usable else None},
        "tracker_sync": {"pending_or_error": pending_sync if queue_ok else None,
                         "unknown_state": sync_unknown if queue_ok else None,
                         "oldest_age_seconds": sync_ages["oldest_age_seconds"] if queue_usable else None,
                         "age_unknown_count": sync_ages["age_unknown_count"] if queue_usable else None},
        "snapshot_gaps": {"known": snapshot_gaps if queue_ok else None,
                          "unverifiable_claims": snapshot_unverifiable if queue_ok else None,
                          "total": snapshot_gaps if queue_ok and not snapshot_unverifiable else None,
                          "oldest_age_seconds": snapshot_ages["oldest_age_seconds"] if queue_usable else None,
                          "age_unknown_count": snapshot_ages["age_unknown_count"] if queue_usable else None,
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
        "intake": intake,
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
    ready = report["ready_buffer"]
    attempts = report["attempts"]
    ownership = report["browser_ownership"]
    sync = report["tracker_sync"]
    gaps = report["snapshot_gaps"]
    def show(value):
        return "unavailable" if value is None else str(value)
    discovery = report["intake"]["discovery"]
    enqueue = report["intake"]["enqueue"]
    discovery_counts = (discovery.get("latest") or {}).get("counts", {})
    enqueue_counts = (enqueue.get("latest") or {}).get("counts", {})
    enqueue_admitted = enqueue_counts.get(
        "admitted", enqueue_counts.get("added", enqueue_counts.get("accepted", 0)))
    enqueue_excluded = enqueue_counts.get("excluded", enqueue_counts.get("rejected", 0))
    enqueue_pending_metadata = enqueue_counts.get("pending_metadata", 0)
    discovery_reasons = (discovery.get("latest") or {}).get("reasons", {})
    enqueue_reasons = (enqueue.get("latest") or {}).get("reasons", {})
    def reason_summary(reasons):
        return ", ".join("{}={}".format(code, count)
                         for code, count in sorted(reasons.items())) or "unknown"
    lines = ["Daily pipeline health · {} ({})".format(report["as_of"], report["timezone"]),
             "Confirmed submissions: {} / {} ({} coverage)".format(
                 goal["confirmed"] if goal["confirmed"] is not None else "unavailable",
                 goal["target"], submissions["coverage"]),
             "Ready buffer: {} / {}".format(
                 show(ready["queued"]), ready["target"]),
             "Queue age: ready {}s (unknown {}); blocked {} (oldest {}s); unknown {} (oldest {}s)".format(
                 show(ready["oldest_age_seconds"]), show(ready["age_unknown_count"]),
                 show(attempts["blocked"]), show(attempts["oldest_blocked_age_seconds"]),
                 show(attempts["outcome_unknown"]), show(attempts["oldest_unknown_age_seconds"])),
             "Attempts: stale {}, in progress {}, outcome unknown {}".format(
                 attempts["stale_in_progress"], attempts["in_progress"], attempts["outcome_unknown"]),
             "Browser owner: active {}, released {}, unknown {}".format(
                 show(ownership["active"]), show(ownership["released"]), show(ownership["unknown"])),
             "Tracker sync pending/error: {} (oldest {}s); snapshot gaps: {} (oldest {}s)".format(
                 sync["pending_or_error"], show(sync["oldest_age_seconds"]),
                 gaps["total"], show(gaps["oldest_age_seconds"])),
             "Intake: discovery {} ({}/{} processed/accepted, age {}s); enqueue {} "
             "(admitted {}, excluded {}, pending metadata {}, active work {}, closed history {}, age {}s); "
             "reasons discovery [{}], enqueue [{}]".format(
                 discovery["status"], discovery_counts.get("processed", 0),
                 discovery_counts.get("accepted", 0), show(discovery["age_seconds"]),
                 enqueue["status"], enqueue_admitted, enqueue_excluded, enqueue_pending_metadata,
                 enqueue_counts.get("active_work", 0), enqueue_counts.get("closed_history", 0),
                 show(enqueue["age_seconds"]), reason_summary(discovery_reasons),
                 reason_summary(enqueue_reasons)),
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
