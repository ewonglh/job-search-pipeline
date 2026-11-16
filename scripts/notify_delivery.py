#!/usr/bin/env python3
"""Optional OpenClaw history adapter for exact notification receipts.

This module never sends a message.  A caller stages an upstream-approved
payload, emits its persisted marker and message, then reconciles native run
history after OpenClaw records the completed run.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import notify_state as N
from config import value_setting


NATIVE_SCHEMA = "openclaw-automation-runs-offset-v1"
OPENCLAW_BIN = value_setting("JOBSEARCH_OPENCLAW_BIN", "openclaw")
PAGE_SIZE = 50
MAX_PAGES = 40
MAX_AGE_SECONDS = 24 * 60 * 60
TIME_TOLERANCE_SECONDS = 5
MAX_RUN_DURATION_MS = 24 * 60 * 60 * 1000
MAX_NATIVE_TIMESTAMP_MS = 10**13
MAX_PAGE_BYTES = 2 * 1024 * 1024
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
MARKER_RE = re.compile(r"^NOTIFY_BATCH=([0-9a-f]{32})$", re.M)
MARKER_LINE_RE = re.compile(r"^NOTIFY_BATCH=.*$", re.M)


class HistoryError(ValueError):
    """Native history is unavailable, incomplete, or outside the supported schema."""


def _job_id(value=None):
    value = os.environ.get("JOBSEARCH_NOTIFY_JOB_ID", "") if value is None else value
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value.strip()):
        raise HistoryError("set JOBSEARCH_NOTIFY_JOB_ID to the native automation job id")
    return value.strip()


def _epoch(value=None):
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


def _row_time(row):
    value = row.get("runAtMs")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > MAX_NATIVE_TIMESTAMP_MS:
        return None
    return value


def _native_identity(row, job_id):
    for field in ("runId", "sessionId"):
        value = row.get(field)
        if isinstance(value, str) and value.strip() and len(value) <= 256:
            digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()
            return field + ":" + digest
    return None


def fetch_history(job_id=None, executable=None, runner=subprocess.run,
                  page_size=PAGE_SIZE, max_pages=MAX_PAGES):
    """Read all bounded pages and reject a history that shifts during pagination."""
    job_id = _job_id(job_id)
    executable = OPENCLAW_BIN if executable is None else executable
    if not isinstance(executable, str) or not executable.strip():
        raise HistoryError("JOBSEARCH_OPENCLAW_BIN must be a non-empty executable")
    if (not isinstance(page_size, int) or page_size < 1
            or not isinstance(max_pages, int) or max_pages < 1):
        raise HistoryError("native history bounds are invalid")
    rows = []
    offset = 0
    first_page = None

    def read_page(page_offset):
        argv = [executable, "cron", "runs", job_id, "--limit",
                str(page_size), "--offset", str(page_offset), "--json"]
        try:
            result = runner(argv, capture_output=True, text=True, timeout=15)
        except Exception as exc:
            raise HistoryError("native history query failed") from exc
        if getattr(result, "returncode", 1) != 0:
            raise HistoryError("native history query failed")
        output = getattr(result, "stdout", None)
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_PAGE_BYTES:
            raise HistoryError("native history page exceeds its size bound")
        try:
            page = json.loads(output)
        except (TypeError, ValueError) as exc:
            raise HistoryError("native history page is not JSON") from exc
        if (not isinstance(page, dict) or not isinstance(page.get("entries"), list)
                or not isinstance(page.get("hasMore"), bool)):
            raise HistoryError("unsupported native history page")
        entries = page["entries"]
        if len(entries) > page_size or any(not isinstance(row, dict) for row in entries):
            raise HistoryError("unsupported native history entries")
        return page

    for _page in range(max_pages):
        page = read_page(offset)
        if first_page is None:
            first_page = page
        entries = page["entries"]
        rows.extend(entries)
        if not page["hasMore"]:
            # Offset pages are not necessarily snapshot-isolated.  If the
            # newest page changed while reading, leave state untouched.
            if read_page(0) != first_page:
                raise HistoryError("native history changed during pagination")
            return rows
        next_offset = page.get("nextOffset")
        if (not isinstance(next_offset, int) or isinstance(next_offset, bool)
                or not entries or next_offset != offset + len(entries)):
            raise HistoryError("native history pagination did not advance")
        offset = next_offset
    raise HistoryError("native history exceeded the configured page bound")


def _batch_time(batch):
    try:
        value = N.parse_timestamp(batch["created_at"])
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _receipt_match(batch_id, batch, receipt, job_id, now, max_age_seconds):
    """Return a reason-free candidate identity only for an exact timed match."""
    if receipt.get("jobId") != job_id or receipt.get("action") != "finished":
        return None
    summary = receipt.get("summary")
    if not isinstance(summary, str):
        return None
    marker_lines = MARKER_LINE_RE.findall(summary)
    markers = MARKER_RE.findall(summary)
    if marker_lines != ["NOTIFY_BATCH=" + batch_id] or markers != [batch_id]:
        return None
    message = batch.get("message")
    expected = "NOTIFY_BATCH=" + batch_id + "\n" + str(message)
    if not isinstance(message, str) or not message.strip():
        return None
    wire_message = expected + ("" if message.endswith("\n") else "\n")
    if summary not in (expected, wire_message):
        return None
    created = _batch_time(batch)
    started = _row_time(receipt)
    duration = receipt.get("durationMs")
    identity = _native_identity(receipt, job_id)
    if (created is None or started is None or identity is None
            or isinstance(duration, bool) or not isinstance(duration, int)
            or duration < 0 or duration > MAX_RUN_DURATION_MS):
        return None
    current = _epoch(now)
    if created > current + TIME_TOLERANCE_SECONDS:
        return None
    if started > (current + TIME_TOLERANCE_SECONDS) * 1000:
        return None
    begin = started / 1000.0
    end = (started + duration) / 1000.0
    if end > current + TIME_TOLERANCE_SECONDS:
        return None
    if not begin - TIME_TOLERANCE_SECONDS <= created <= end + TIME_TOLERANCE_SECONDS:
        return None
    if current - created > max_age_seconds:
        return None
    return identity


def receipt_matches(batch_id, batch, receipt, job_id=None, now=None,
                    max_age_seconds=MAX_AGE_SECONDS):
    """Public predicate used by offline callers and tests."""
    try:
        return _receipt_match(batch_id, batch, receipt, _job_id(job_id), now,
                              max_age_seconds) is not None
    except HistoryError:
        return False


def reconcile_state(state, receipts, job_id=None, now=None,
                    max_age_seconds=MAX_AGE_SECONDS):
    """Update discovery progress and pending/ack records from a complete history read."""
    job_id = _job_id(job_id)
    current = _epoch(now)
    if not isinstance(receipts, list) or any(not isinstance(row, dict) for row in receipts):
        raise HistoryError("unsupported native history entries")

    # Discovery progress is independent from model decisions and batch status.
    discovered = []
    latest = state["discovery"]["latest_run_at_ms"]
    for row in receipts:
        stamp = _row_time(row)
        identity = _native_identity(row, job_id)
        if stamp is not None:
            latest = max(latest, stamp)
        if stamp is not None and identity:
            discovered.append((stamp, identity))
    seen = set(state["discovery"]["run_ids"])
    merged = [(0, item) for item in state["discovery"]["run_ids"]]
    for stamp, identity in discovered:
        if identity not in seen:
            merged.append((stamp, identity))
            seen.add(identity)
    merged = sorted(merged, key=lambda pair: pair[0], reverse=True)
    state["discovery"] = {
        "last_scan_at": datetime.fromtimestamp(current, timezone.utc).isoformat(timespec="seconds"),
        "latest_run_at_ms": latest,
        "run_ids": [identity for _stamp, identity in merged[:N.MAX_DISCOVERED_RUNS]],
    }

    review = []
    acknowledged = []
    for batch_id, batch in list(state["pending_batches"].items()):
        if batch.get("review_reason") == "legacy_batch_requires_review":
            review.append(batch_id)
            continue
        created = _batch_time(batch)
        if created is None:
            batch["review_reason"] = "invalid_batch_time"
            review.append(batch_id)
            continue
        age = current - created
        if age > max_age_seconds:
            batch["review_reason"] = "expired_receipt_window"
            review.append(batch_id)
            continue
        matches = []
        for receipt in receipts:
            identity = _receipt_match(batch_id, batch, receipt, job_id,
                                      current, max_age_seconds)
            if identity is not None:
                matches.append((receipt, identity))
        if len(matches) != 1:
            batch["review_reason"] = ("ambiguous_receipt" if len(matches) > 1
                                      else "receipt_missing")
            review.append(batch_id)
            continue
        receipt, identity = matches[0]
        previous_identity = batch.get("native_run_id")
        if previous_identity is not None and previous_identity != identity:
            batch["review_reason"] = "contradictory_run_identity"
            review.append(batch_id)
            continue
        batch["native_run_id"] = identity
        if (receipt.get("status") != "ok"
                or receipt.get("completionStatus") not in (None, "succeeded")
                or receipt.get("error")):
            batch["review_reason"] = "run_not_successful"
            review.append(batch_id)
            continue
        delivered = receipt.get("delivered")
        delivery_status = receipt.get("deliveryStatus")
        if delivered is True and delivery_status == "delivered":
            if len(state["acknowledgments"]) >= N.MAX_ACKNOWLEDGMENTS:
                raise N.StateError("notification acknowledgment history reached its bound")
            state["acknowledgments"][batch_id] = {
                "acknowledged_at": datetime.fromtimestamp(
                    current, timezone.utc).isoformat(timespec="seconds"),
                "native_run_id": identity,
                "message_sha256": hashlib.sha256(batch["message"].encode("utf-8")).hexdigest(),
            }
            state["decisions"][batch["decision_id"]]["resolution"] = "delivered"
            state["decisions"][batch["decision_id"]]["resolved_at"] = state[
                "acknowledgments"][batch_id]["acknowledged_at"]
            state["decisions"][batch["decision_id"]]["resolution_reason"] = "native_delivered"
            del state["pending_batches"][batch_id]
            acknowledged.append(batch_id)
            continue
        if (delivered is True) != (delivery_status == "delivered"):
            batch["review_reason"] = "contradictory_delivery_evidence"
        elif delivered is False or delivery_status == "failed":
            batch["review_reason"] = "delivery_failed"
        else:
            batch["review_reason"] = "delivery_unconfirmed"
        review.append(batch_id)
    N._validate(state)
    return {"acknowledged": acknowledged, "needs_review": review,
            "pending": len(state["pending_batches"])}


def _read_fixture(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            fixture = json.load(stream)
    except (OSError, ValueError) as exc:
        raise HistoryError("receipt fixture cannot be read") from exc
    if (not isinstance(fixture, dict) or fixture.get("schema") != NATIVE_SCHEMA
            or fixture.get("complete") is not True
            or not isinstance(fixture.get("entries"), list)
            or any(not isinstance(row, dict) for row in fixture["entries"])):
        raise HistoryError("receipt fixture is incomplete or unsupported")
    return fixture["entries"]


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--stage-file", help="UTF-8 message text to stage after an explicit gate decision")
    modes.add_argument("--reconcile", action="store_true", help="reconcile pending batches against native history")
    modes.add_argument("--record-decision", action="store_true",
                       help="record a suppressed or silent upstream gate outcome")
    modes.add_argument("--resolve-batch", help="dismiss a reviewed notice without claiming delivery")
    modes.add_argument("--compact-before", help="discard terminal details before this UTC time; retain at least 30 days")
    parser.add_argument("--decision-id", help="stable, non-sensitive upstream decision id")
    parser.add_argument("--decision-at", help="immutable upstream event timestamp, with UTC offset")
    parser.add_argument("--decision", choices=("deliver", "suppressed", "silent"),
                        help="upstream gate outcome; this command makes no model call")
    parser.add_argument("--receipts-fixture", help="complete synthetic receipt fixture; never queries native history")
    parser.add_argument("--dry-run", action="store_true",
                        help="read only; never fetch native history, lock, persist, send, or emit a real marker")
    parser.add_argument("--state", default=N.STATE_PATH, help="runtime state path (env JOBSEARCH_NOTIFY_STATE)")
    return parser


def _emit_json(value):
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.resolve_batch or args.compact_before:
        if args.decision_id or args.decision or args.decision_at or args.receipts_fixture:
            print("notify_delivery: maintenance cannot include decision or receipt options", file=sys.stderr)
            return 2
        def maintain(state):
            return (N.resolve_batch(state, args.resolve_batch) if args.resolve_batch
                    else N.compact_state(state, args.compact_before))
        try:
            report = maintain(N.load_state(args.state)) if args.dry_run else N.update_state(args.state, maintain)
            _emit_json(dict(report, dry_run=args.dry_run))
            return 0
        except (OSError, ValueError) as exc:
            print("notify_delivery: maintenance held (" + type(exc).__name__ + ")", file=sys.stderr)
            return 2
    if args.record_decision or args.stage_file:
        try:
            if not args.decision_at or N.parse_timestamp(args.decision_at).tzinfo is None:
                raise ValueError("missing timestamp")
            decision_at = N.parse_timestamp(args.decision_at)
            if decision_at.timestamp() > _epoch() + TIME_TOLERANCE_SECONDS:
                raise ValueError("future timestamp")
        except ValueError:
            print("notify_delivery: --decision-at requires the immutable upstream timestamp with UTC offset", file=sys.stderr)
            return 2
    if args.record_decision:
        if (not args.decision_id or args.decision not in ("suppressed", "silent")
                or args.receipts_fixture):
            print("notify_delivery: --record-decision requires an id and a suppressed or silent decision",
                  file=sys.stderr)
            return 2
        if args.dry_run:
            try:
                N.record_decision(N.load_state(args.state), args.decision_id,
                                  args.decision, decision_at)
            except (OSError, N.StateError):
                print("notify_delivery: decision preview held", file=sys.stderr)
                return 2
            _emit_json({"dry_run": True, "decision": args.decision,
                        "would_record": True, "writes": False})
            return 0
        try:
            N.update_state(args.state, lambda state: N.record_decision(
                state, args.decision_id, args.decision, decision_at))
            _emit_json({"recorded": True, "decision": args.decision})
            return 0
        except (OSError, N.StateError) as exc:
            print("notify_delivery: decision held (" + type(exc).__name__ + ")",
                  file=sys.stderr)
            return 2
    if args.stage_file:
        if not args.decision_id or args.decision != "deliver" or args.receipts_fixture:
            print("notify_delivery: staging requires --decision-id and --decision deliver",
                  file=sys.stderr)
            return 2
        try:
            with open(args.stage_file, "r", encoding="utf-8") as stream:
                message = stream.read()
            if args.dry_run:
                if args.receipts_fixture:
                    raise HistoryError("receipt fixtures apply only to --reconcile")
                preview = N.load_state(args.state)
                N.record_decision(preview, args.decision_id, args.decision, decision_at)
                N.add_pending_batch(preview, args.decision_id, message)
                _emit_json({"dry_run": True, "decision": args.decision,
                            "would_stage": args.decision == "deliver",
                            "payload_emitted": False,
                            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()})
                return 0
            batch_id = None
            newly_staged = False

            def mutate(state):
                nonlocal batch_id, newly_staged
                N.record_decision(state, args.decision_id, args.decision, decision_at)
                if args.decision == "deliver":
                    newly_staged = state["decisions"][args.decision_id]["batch_id"] is None
                    batch_id = N.add_pending_batch(state, args.decision_id, message)

            N.update_state(args.state, mutate)
            if not newly_staged:
                _emit_json({"staged": False, "already_recorded": True,
                            "batch_id": batch_id, "payload_emitted": False})
                return 0
            # update_state completed its atomic write before the marker is exposed.
            sys.stdout.write("NOTIFY_BATCH=" + batch_id + "\n" + message)
            if not message.endswith("\n"):
                sys.stdout.write("\n")
            return 0
        except (OSError, N.StateError, HistoryError) as exc:
            print("notify_delivery: staging held (" + type(exc).__name__ + ")", file=sys.stderr)
            return 2

    if args.decision_id or args.decision or args.decision_at:
        print("notify_delivery: --decision options apply only to --stage-file", file=sys.stderr)
        return 2
    try:
        state = N.load_state(args.state)
        if args.dry_run and not args.receipts_fixture:
            _emit_json({"dry_run": True, "native_history": "not_fetched",
                        "pending": len(state["pending_batches"]), "writes": False})
            return 0
        if args.receipts_fixture:
            receipts = _read_fixture(args.receipts_fixture)
        elif not state["pending_batches"]:
            _emit_json({"reconciled": False, "reason": "no_pending_batches", "pending": 0})
            return 0
        elif args.dry_run:
            raise HistoryError("dry-run does not query native history")
        else:
            receipts = fetch_history()

        job_id = _job_id()
        if args.dry_run:
            preview = json.loads(json.dumps(state))
            report = reconcile_state(preview, receipts, job_id)
            _emit_json({"dry_run": True, "writes": False, **report})
            return 0
        report = N.update_state(
            args.state,
            lambda current: reconcile_state(current, receipts, job_id))
        _emit_json({"reconciled": True, **report})
        return 0
    except (OSError, N.StateError, HistoryError) as exc:
        print("notify_delivery: reconciliation held (" + type(exc).__name__ + ")",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
