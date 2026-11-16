#!/usr/bin/env python3
"""Synthetic-only tests for bounded native notification receipts."""
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import notify_delivery as D
import notify_state as N


JOB = "synthetic-job-9"
CREATED = datetime.datetime(2026, 11, 16, 3, 0, 0,
                            tzinfo=datetime.timezone.utc)
NOW = CREATED.timestamp() + 120
MESSAGE = "Synthetic digest: 2 items need review."
BATCH_ID = "a" * 32


def completed_page(entries, has_more, next_offset=None):
    body = {"entries": entries, "hasMore": has_more}
    if next_offset is not None:
        body["nextOffset"] = next_offset
    return mock.Mock(returncode=0, stdout=json.dumps(body))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.temp.name, "notify.json")
        self.state = N.empty_state()
        N.record_decision(self.state, "event-1", "deliver", CREATED)
        self.batch_id = N.add_pending_batch(self.state, "event-1", MESSAGE,
                                            CREATED, BATCH_ID)
        self.receipt = self.make_receipt()

    def tearDown(self):
        self.temp.cleanup()

    def make_receipt(self, **updates):
        row = {
            "jobId": JOB,
            "action": "finished",
            "status": "ok",
            "completionStatus": "succeeded",
            "runId": "synthetic-run-17",
            "runAtMs": int((CREATED.timestamp() - 1) * 1000),
            "durationMs": 5000,
            "delivered": True,
            "deliveryStatus": "delivered",
            "summary": "NOTIFY_BATCH=" + BATCH_ID + "\n" + MESSAGE,
        }
        row.update(updates)
        return row

    def new_state(self):
        state = json.loads(json.dumps(self.state))
        return state

    def test_exact_wrong_job_wrong_batch_ambiguous_and_failed_receipts(self):
        cases = [
            ("exact", [self.receipt], True, None),
            ("wrong_job", [self.make_receipt(jobId="other-job")], False,
             "receipt_missing"),
            ("wrong_batch", [self.make_receipt(summary="NOTIFY_BATCH=" + "b" * 32
                                                + "\n" + MESSAGE)], False,
             "receipt_missing"),
            ("ambiguous", [self.receipt, self.receipt], False,
             "ambiguous_receipt"),
            ("run_only", [self.make_receipt(delivered=None,
                                            deliveryStatus=None)], False,
             "delivery_unconfirmed"),
            ("failed", [self.make_receipt(delivered=False,
                                          deliveryStatus="failed")], False,
             "delivery_failed"),
            ("contradictory", [self.make_receipt(delivered=True,
                                                  deliveryStatus="failed")], False,
             "contradictory_delivery_evidence"),
            ("run_error", [self.make_receipt(status="error")], False, "run_not_successful"),
            ("run_skipped", [self.make_receipt(status="skipped")], False, "run_not_successful"),
            ("run_unknown", [self.make_receipt(status=None)], False, "run_not_successful"),
            ("completion_failed", [self.make_receipt(completionStatus="failed")],
             False, "run_not_successful"),
        ]
        for name, rows, expected_ack, expected_reason in cases:
            with self.subTest(name=name):
                state = self.new_state()
                result = D.reconcile_state(state, rows, JOB, NOW)
                self.assertEqual(bool(result["acknowledged"]), expected_ack)
                if expected_ack:
                    self.assertNotIn(self.batch_id, state["pending_batches"])
                    self.assertIn(self.batch_id, state["acknowledgments"])
                else:
                    self.assertIn(self.batch_id, state["pending_batches"])
                    self.assertEqual(state["pending_batches"][self.batch_id]["review_reason"],
                                     expected_reason)

    def test_incomplete_message_bad_timing_and_missing_run_identity_stay_pending(self):
        cases = [
            self.make_receipt(summary="NOTIFY_BATCH=" + BATCH_ID + "\nSynthetic digest:"),
            self.make_receipt(summary=self.receipt["summary"] + "\nUNRELATED TEXT"),
            self.make_receipt(summary="PREFIX\n" + self.receipt["summary"]),
            self.make_receipt(runAtMs=int((CREATED.timestamp() - 100) * 1000)),
            self.make_receipt(runId=None, sessionId=None, runAtMs=None),
            self.make_receipt(runId=None, sessionId=None),
            self.make_receipt(durationMs=1000000),
        ]
        for row in cases:
            state = self.new_state()
            D.reconcile_state(state, [row], JOB, NOW)
            self.assertIn(self.batch_id, state["pending_batches"])
            self.assertNotIn(self.batch_id, state["acknowledgments"])

    def test_failed_receipt_can_resolve_only_for_the_same_native_run(self):
        state = self.new_state()
        failed = self.make_receipt(delivered=False, deliveryStatus="failed")
        D.reconcile_state(state, [failed], JOB, NOW)
        self.assertTrue(state["pending_batches"][self.batch_id]["native_run_id"].startswith(
            "runId:"))
        D.reconcile_state(state, [self.receipt], JOB, NOW + 10)
        self.assertIn(self.batch_id, state["acknowledgments"])

        state = self.new_state()
        D.reconcile_state(state, [failed], JOB, NOW)
        contradictory = self.make_receipt(runId="different-run")
        D.reconcile_state(state, [contradictory], JOB, NOW + 10)
        self.assertIn(self.batch_id, state["pending_batches"])
        self.assertEqual(state["pending_batches"][self.batch_id]["review_reason"],
                         "contradictory_run_identity")

    def test_expired_batch_remains_visible_and_silent_decision_is_not_delivery_failure(self):
        state = self.new_state()
        report = D.reconcile_state(state, [self.receipt], JOB,
                                   CREATED.timestamp() + D.MAX_AGE_SECONDS + 1)
        self.assertEqual(report["acknowledged"], [])
        self.assertEqual(state["pending_batches"][self.batch_id]["review_reason"],
                         "expired_receipt_window")
        N.record_decision(state, "event-silent", "silent", CREATED)
        self.assertNotIn("event-silent", state["pending_batches"])

    def test_paginated_burst_survives_restart_and_reconciles_later_page(self):
        N.update_state(self.state_path, lambda state: (
            N.record_decision(state, "event-1", "deliver", CREATED),
            N.add_pending_batch(state, "event-1", MESSAGE, CREATED, BATCH_ID)))
        # Reloading here models a process restart after durable staging.
        restarted = N.load_state(self.state_path)
        self.assertIn(BATCH_ID, restarted["pending_batches"])
        rows = [self.make_receipt(runId="burst-%03d" % index,
                                  summary="no notification in this run")
                for index in range(6)]
        rows.append(self.receipt)
        pages = [
            completed_page(rows[0:3], True, 3),
            completed_page(rows[3:6], True, 6),
            completed_page(rows[6:7], False),
            completed_page(rows[0:3], True, 3),
        ]
        runner = mock.Mock(side_effect=pages)
        fetched = D.fetch_history(JOB, "fake-openclaw", runner,
                                  page_size=3, max_pages=3)
        self.assertEqual(len(fetched), 7)
        self.assertEqual(runner.call_count, 4)
        self.assertEqual([call.args[0][7] for call in runner.call_args_list],
                         ["0", "3", "6", "0"])
        report = D.reconcile_state(restarted, fetched, JOB, NOW)
        self.assertEqual(report["acknowledged"], [BATCH_ID])
        N.update_state(self.state_path, lambda state: D.reconcile_state(
            state, fetched, JOB, NOW))
        final = N.load_state(self.state_path)
        self.assertIn(BATCH_ID, final["acknowledgments"])
        self.assertEqual(len(final["discovery"]["run_ids"]), 7)

    def test_broken_pagination_fails_closed_without_mutating_pending_state(self):
        before = self.new_state()
        with self.assertRaises(D.HistoryError):
            D.fetch_history(JOB, "fake-openclaw",
                            mock.Mock(return_value=completed_page([self.receipt], True)),
                            page_size=1, max_pages=2)
        after = self.new_state()
        self.assertEqual(before["pending_batches"], after["pending_batches"])
        self.assertEqual(before["acknowledgments"], after["acknowledgments"])

    def test_history_shift_during_pagination_fails_closed(self):
        rows = [self.make_receipt(runId="page-%s" % value) for value in ("a", "b", "c")]
        pages = [completed_page(rows[:1], True, 1),
                 completed_page(rows[1:2], False),
                 completed_page([self.make_receipt(runId="newer")], True, 1)]
        with self.assertRaises(D.HistoryError):
            D.fetch_history(JOB, "fake-openclaw", mock.Mock(side_effect=pages),
                            page_size=1, max_pages=2)

    def test_state_bounds_reject_extra_pending_batches_and_oversized_messages(self):
        state = N.empty_state()
        for index in range(N.MAX_PENDING_BATCHES):
            decision_id = "event-bound-%d" % index
            N.record_decision(state, decision_id, "deliver", CREATED)
            N.add_pending_batch(state, decision_id, "Synthetic bounded message",
                                CREATED, "%032x" % (index + 1))
        N.record_decision(state, "event-extra", "deliver", CREATED)
        with self.assertRaises(N.StateError):
            N.add_pending_batch(state, "event-extra", "another message", CREATED,
                                "%032x" % (N.MAX_PENDING_BATCHES + 1))
        self.assertEqual(len(state["pending_batches"]), N.MAX_PENDING_BATCHES)
        oversized = N.empty_state()
        N.record_decision(oversized, "event-bound", "deliver", CREATED)
        with self.assertRaises(N.StateError):
            N.add_pending_batch(oversized, "event-bound", "x" * (N.MAX_MESSAGE_BYTES + 1),
                                CREATED, "f" * 32)

    def test_legacy_positions_are_quarantined_without_copying_position_values(self):
        legacy_batch = "c" * 32
        legacy = {"version": 2, "cursors": {"synthetic-old-position": 731},
                  "events": {"not-migrated": {"synthetic": True}},
                  "batches": {legacy_batch: {
                      "id": legacy_batch, "status": "unknown", "run_id": None,
                      "items": ["synthetic-item"], "text": "Synthetic legacy notice",
                      "created_at": CREATED.strftime("%Y-%m-%dT%H:%M:%S%z")}}}
        with open(self.state_path, "w", encoding="utf-8") as stream:
            json.dump(legacy, stream)
        state = N.load_state(self.state_path)
        self.assertEqual(state["legacy_quarantine"][0]["status"], "quarantined")
        self.assertEqual(state["legacy_quarantine"][0]["cursor_count"], 1)
        self.assertIn(legacy_batch, state["pending_batches"])
        self.assertEqual(state["pending_batches"][legacy_batch]["review_reason"],
                         "legacy_batch_requires_review")
        N.update_state(self.state_path, lambda current: N.record_decision(
            current, "event-after-migration", "silent", CREATED))
        with open(self.state_path, "r", encoding="utf-8") as stream:
            persisted = stream.read()
        self.assertNotIn("synthetic-old-position", persisted)
        self.assertNotIn("731", persisted)
        self.assertNotIn("not-migrated", persisted)
        after_migration = N.load_state(self.state_path)
        legacy_receipt = {
            "jobId": JOB, "action": "finished", "runId": "synthetic-legacy-run",
            "runAtMs": int((CREATED.timestamp() - 1) * 1000),
            "durationMs": 5000, "delivered": True,
            "deliveryStatus": "delivered",
            "summary": "NOTIFY_BATCH=" + legacy_batch + "\nSynthetic legacy notice",
        }
        D.reconcile_state(after_migration, [legacy_receipt], JOB, NOW)
        self.assertIn(legacy_batch, after_migration["pending_batches"])
        self.assertNotIn(legacy_batch, after_migration["acknowledgments"])

    def test_staging_persists_before_emitting_marker(self):
        message_path = os.path.join(self.temp.name, "message.txt")
        with open(message_path, "w", encoding="utf-8") as stream:
            stream.write(MESSAGE)

        class CheckingOutput(io.StringIO):
            def write(inner_self, value):
                persisted = N.load_state(self.state_path)
                self.assertEqual(len(persisted["pending_batches"]), 1)
                return super(CheckingOutput, inner_self).write(value)

        output = CheckingOutput()
        with contextlib.redirect_stdout(output):
            code = D.main(["--stage-file", message_path, "--decision-id", "event-stage",
                           "--decision", "deliver", "--decision-at", CREATED.isoformat(),
                           "--state", self.state_path])
        self.assertEqual(code, 0)
        payload = output.getvalue()
        persisted = N.load_state(self.state_path)
        batch_id = next(iter(persisted["pending_batches"]))
        self.assertTrue(payload.startswith("NOTIFY_BATCH=" + batch_id + "\n" + MESSAGE))

    def test_retry_binding_survives_restart_acknowledgment_and_changed_payload(self):
        message_path = os.path.join(self.temp.name, "message.txt")
        with open(message_path, "w", encoding="utf-8") as stream:
            stream.write(MESSAGE)
        argv = ["--stage-file", message_path, "--decision-id", "event-retry",
                "--decision", "deliver", "--decision-at", CREATED.isoformat(),
                "--state", self.state_path]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(D.main(argv), 0)
        before = N.load_state(self.state_path)
        batch_id = before["decisions"]["event-retry"]["batch_id"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(D.main(argv), 0)
        self.assertNotIn("NOTIFY_BATCH=", output.getvalue())
        self.assertEqual(N.load_state(self.state_path), before)
        with open(message_path, "w", encoding="utf-8") as stream:
            stream.write(MESSAGE + " changed")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(D.main(argv), 2)
        self.assertEqual(N.load_state(self.state_path), before)
        with open(message_path, "w", encoding="utf-8") as stream:
            stream.write(MESSAGE)
        receipt = self.make_receipt(summary="NOTIFY_BATCH=" + batch_id + "\n" + MESSAGE,
                                    runAtMs=int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
                                    durationMs=0)
        N.update_state(self.state_path, lambda state: D.reconcile_state(state, [receipt], JOB))
        self.assertIn(batch_id, N.load_state(self.state_path)["acknowledgments"])
        with contextlib.redirect_stdout(output):
            self.assertEqual(D.main(argv), 0)
        self.assertEqual(N.load_state(self.state_path)["pending_batches"], {})

    def test_review_closure_compaction_and_replay_floor(self):
        state = self.new_state()
        later = CREATED + datetime.timedelta(days=65)
        cutoff = CREATED + datetime.timedelta(days=32)
        N.record_decision(state, "old-suppressed", "suppressed", CREATED)
        N.record_decision(state, "recent", "silent", later)
        D.reconcile_state(state, [], JOB, later.timestamp())
        N.compact_state(state, cutoff.isoformat(), later)
        self.assertIn(BATCH_ID, state["pending_batches"])
        self.assertNotIn("old-suppressed", state["decisions"])
        resolved = N.resolve_batch(state, BATCH_ID, later)
        self.assertEqual(resolved["delivery"], "unknown")
        self.assertEqual(state["acknowledgments"], {})
        N.compact_state(state, cutoff.isoformat(), later)
        self.assertIn("event-1", state["decisions"])
        much_later = later + datetime.timedelta(days=65)
        N.compact_state(state, (later + datetime.timedelta(days=32)).isoformat(), much_later)
        self.assertEqual(state["decisions"], {})
        with self.assertRaises(N.StateError):
            N.record_decision(state, "event-1", "deliver", CREATED)
        with self.assertRaises(N.StateError):
            N.compact_state(state, (later - datetime.timedelta(days=1)).isoformat(), later)
        N._validate(state)

    def test_acknowledged_retention_releases_capacity_without_historical_replay(self):
        state = self.new_state()
        D.reconcile_state(state, [self.receipt], JOB, NOW)
        later = CREATED + datetime.timedelta(days=65)
        N.compact_state(state, (CREATED + datetime.timedelta(days=32)).isoformat(), later)
        self.assertEqual(state["decisions"], {})
        self.assertEqual(state["acknowledgments"], {})
        with self.assertRaises(N.StateError):
            N.record_decision(state, "event-1", "deliver", CREATED)
        N.record_decision(state, "fresh-decision", "silent", later)
        N._validate(state)

    def test_dry_run_with_fixture_leaves_state_fixture_locks_and_diagnostics_unchanged(self):
        N.update_state(self.state_path, lambda state: (
            N.record_decision(state, "event-1", "deliver", CREATED),
            N.add_pending_batch(state, "event-1", MESSAGE, CREATED, BATCH_ID)))
        fixture_path = os.path.join(self.temp.name, "receipts.json")
        fixture = {"schema": D.NATIVE_SCHEMA, "complete": True,
                   "entries": [self.receipt]}
        with open(fixture_path, "w", encoding="utf-8") as stream:
            json.dump(fixture, stream)
        def read_files():
            files = {}
            for name in os.listdir(self.temp.name):
                with open(os.path.join(self.temp.name, name), "rb") as stream:
                    files[name] = stream.read()
            return files

        before = read_files()
        before_names = set(os.listdir(self.temp.name))
        with mock.patch.dict(os.environ, {"JOBSEARCH_NOTIFY_JOB_ID": JOB}):
            with mock.patch.object(D, "fetch_history", side_effect=AssertionError("live fetch")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = D.main(["--reconcile", "--dry-run", "--receipts-fixture",
                                   fixture_path, "--state", self.state_path])
        self.assertEqual(code, 0)
        self.assertIn('"dry_run": true', output.getvalue())
        after_names = set(os.listdir(self.temp.name))
        after = read_files()
        self.assertEqual(before_names, after_names)
        self.assertEqual(before, after)
        self.assertIn("notify.json.lock", after_names)
        absent_state = os.path.join(self.temp.name, "never-created.json")
        with contextlib.redirect_stdout(io.StringIO()):
            code = D.main(["--reconcile", "--dry-run", "--state", absent_state])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(absent_state))
        self.assertFalse(os.path.exists(absent_state + ".lock"))


if __name__ == "__main__":
    unittest.main()
