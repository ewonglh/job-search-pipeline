#!/usr/bin/env python3
"""Record and review dated recruitment outcomes with an auditable ledger."""
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

import application_identity as identity
import recruitment_evidence as evidence
import stages
from config import default_repo_dir, default_runtime_path, path_setting


TRACKER = path_setting("JOBSEARCH_TRACKER", Path(default_repo_dir()) / "applications.yaml")
LEDGER = path_setting("JOBSEARCH_RECRUITMENT_LEDGER",
                      default_runtime_path("recruitment_events.json"))
REVIEW_TYPES = {"availability_request", "recruiter_reply", "oa_invitation",
                "oa_completed", "oa_passed", "oa_failed", "interview_invitation",
                "interview_completed", "offer", "rejection"}
RECORD_TYPES = evidence.EVENT_TYPES - {"account_admin", "job_alert", "unrelated", "unclassified"}


def row_for_pr(tracker, number):
    _, entries = stages.read_apps(str(tracker))
    hits = [entry["row"] for entry in entries
            if identity.pr_number(entry["row"]) == number]
    if len(hits) != 1:
        raise ValueError("PR must identify exactly one tracker application")
    return hits[0]


def _load_ledger(path, missing_ok=False):
    if missing_ok and not path.exists():
        return {"schema": 1, "events": [], "coverage": {}, "review_required": []}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("events"), list):
        raise ValueError("unsupported or invalid evidence ledger")
    return doc


def _event_day(value):
    parsed = evidence.exact_date(value)
    if not parsed:
        raise ValueError("Exact ISO or RFC date required")
    try:
        return datetime.fromisoformat(parsed.replace("Z", "+00:00")).date(), parsed
    except ValueError as exc:
        raise ValueError("Exact ISO or RFC date required") from exc


def _check_event_day(day, row):
    if day > date.today():
        raise ValueError("Event date is in the future")
    applied = stages.parse_day(row.get("applied_date"))
    if applied and day < applied:
        raise ValueError("Event predates application")


def _evidence_text(value):
    note = evidence.safe_excerpt(value)
    if not note:
        raise ValueError("Nonempty evidence is required")
    return note


def record(args):
    row = row_for_pr(args.tracker, args.pr)
    day, occurred = _event_day(args.date)
    _check_event_day(day, row)
    note = _evidence_text(args.evidence)
    event = {"application_id": identity.application_id(row),
             "event_type": args.type, "occurred_at": occurred,
             "verification": "verified", "origin": args.origin,
             "evidence": note, "source": "manual_record",
             "recorded_at": datetime.now(timezone.utc).isoformat()}
    if args.evidence_file:
        event["receipt_sha256"] = hashlib.sha256(args.evidence_file.read_bytes()).hexdigest()
    semantic = json.dumps({key: value for key, value in event.items()
                           if key != "recorded_at"}, sort_keys=True, ensure_ascii=False)
    action_id = args.id or hashlib.sha256(semantic.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", action_id):
        raise ValueError("Action ID may contain only letters, digits, '.', '_', ':' or '-'")
    event_id = "manual:" + action_id
    event.update(event_id=event_id, message_id=event_id)
    doc = _load_ledger(args.ledger, missing_ok=True)
    # Preserve the original audit timestamp for an identical manual re-entry.
    existing = next((old for old in doc["events"]
                     if old.get("event_id") == event_id
                     and evidence._revision_id(old) == evidence._revision_id(event)), None)
    if existing:
        event["recorded_at"] = existing.get("recorded_at", event["recorded_at"])
    return event


def adjudicate(args):
    doc = _load_ledger(args.ledger)
    event_id = args.event_id
    versions = [evidence._version(item) for item in doc["events"]
                if item.get("event_id") == event_id]
    if not versions:
        raise ValueError("Event ID not found")
    active, quarantined, _ = evidence.effective_events(doc)
    if event_id in quarantined and not args.resolve_conflict:
        raise ValueError("Conflicting evidence requires --resolve-conflict")
    current_items = [item for item in active if item.get("event_id") == event_id]
    original = current_items[-1] if current_items else versions[-1]
    event = dict(original)

    row = None
    if args.pr is not None:
        row = row_for_pr(args.tracker, args.pr)
        event["application_id"] = identity.application_id(row)
    elif event.get("application_id"):
        _, entries = stages.read_apps(str(args.tracker))
        matches = [entry["row"] for entry in entries
                   if identity.application_id(entry["row"]) == event["application_id"]]
        row = matches[0] if len(matches) == 1 else None
    if args.type:
        event["event_type"] = args.type
    if args.date:
        day, occurred = _event_day(args.date)
        event["occurred_at"] = occurred
    else:
        day, _ = _event_day(event.get("occurred_at"))
    if args.origin:
        event["origin"] = args.origin
    if not event.get("application_id"):
        raise ValueError("Adjudication needs an exact application identity; pass --pr")
    if row is None:
        raise ValueError("Adjudication application must match exactly one tracker row")
    _check_event_day(day, row)
    note = _evidence_text(args.evidence)

    changed = any(original.get(key) not in (None, "", "unclassified", "unknown")
                  and original.get(key) != event.get(key)
                  for key in ("application_id", "event_type", "occurred_at", "origin"))
    if changed and not args.resolve_conflict:
        raise ValueError("Changing an established fact requires --resolve-conflict")
    all_ids = {item["revision_id"] for item in versions}
    superseded = {revision for item in versions for revision in item.get("supersedes", [])}
    live_ids = sorted(all_ids - superseded)
    event.update(verification="verified", source="manual_adjudication", evidence=note,
                 adjudicated_at=datetime.now(timezone.utc).isoformat(),
                 resolution="explicit" if args.resolve_conflict else "enrichment",
                 supersedes=live_ids if args.resolve_conflict else [original["revision_id"]])
    for key in ("revision", "revision_id", "verification_reasons", "match_reason"):
        event.pop(key, None)
    return event


def review(args):
    doc = _load_ledger(args.ledger)
    active, quarantined, _ = evidence.effective_events(doc)
    rows = []
    for event in active:
        event_id = str(event.get("event_id") or "")
        actionable = event.get("event_type") in REVIEW_TYPES
        needs = (event.get("verification") != "verified"
                 or not event.get("application_id")
                 or event_id in quarantined
                 or (event.get("event_type") in {"availability_request", "recruiter_reply"}
                     and event.get("origin") != "human"))
        if needs and (args.all or actionable):
            rows.append({key: event.get(key) for key in
                         ("event_id", "event_type", "application_id", "occurred_at",
                          "origin", "verification", "evidence", "match_reason",
                          "verification_reasons")})
    print(json.dumps({"review": rows, "quarantined_event_ids": sorted(quarantined)}, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=Path(LEDGER),
                        help="private evidence ledger (env JOBSEARCH_RECRUITMENT_LEDGER)")
    parser.add_argument("--tracker", type=Path, default=Path(TRACKER),
                        help="applications.yaml (env JOBSEARCH_TRACKER)")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("record", help="record one explicitly evidenced outcome")
    add.add_argument("--pr", type=int, required=True)
    add.add_argument("--type", choices=sorted(RECORD_TYPES), required=True)
    add.add_argument("--date", required=True)
    add.add_argument("--origin", choices=("human", "automated", "unknown"), default="unknown")
    add.add_argument("--id", help="stable action ID for repeat imports")
    add.add_argument("--evidence", required=True)
    add.add_argument("--evidence-file", type=Path,
                     help="hash an explicit receipt; file contents are not copied")
    adj = sub.add_parser("adjudicate", help="append an evidence-backed correction")
    adj.add_argument("--event-id", required=True)
    adj.add_argument("--pr", type=int)
    adj.add_argument("--origin", choices=("human", "automated", "unknown"))
    adj.add_argument("--type", choices=sorted(RECORD_TYPES))
    adj.add_argument("--date")
    adj.add_argument("--evidence", required=True)
    adj.add_argument("--resolve-conflict", action="store_true")
    rev = sub.add_parser("review", help="list actionable evidence needing review")
    rev.add_argument("--all", action="store_true", help="include acknowledgements and unclassified evidence")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "review":
            return review(args)
        event = record(args) if args.command == "record" else adjudicate(args)
        result = evidence.save_ledger(args.ledger, {
            "schema": 1, "events": [event], "review_required": [],
            "coverage": {"complete": False, "source": event["source"],
                         "limitations": ["Manual evidence; mailbox completeness is unknown"]},
        })
        _, quarantined, _ = evidence.effective_events(result)
        print(json.dumps({"event_id": event["event_id"],
                          "application_id": event["application_id"],
                          "event_type": event["event_type"],
                          "conflicted": event["event_id"] in quarantined}))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Outcome recording failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
