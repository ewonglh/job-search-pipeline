#!/usr/bin/env python3
"""Synthetic checks for reversible quiet-routing gates and delivery boundaries."""
import copy
import datetime
import json
import unittest

import notify_state
import notify_delivery
import quiet_routing as Q


REVISION = "a" * 40
CURSOR = "2026-11-16T06:00:00Z"
ACCEPTANCE = {
    "phone": {"accepted": True, "revision": REVISION, "recorded_at": "2026-11-16T05:50:00Z"},
    "desktop": {"accepted": True, "revision": REVISION, "recorded_at": "2026-11-16T05:55:00Z"},
}


def confirmed_delivery(notices):
    state = notify_state.empty_state()
    for index, notice in enumerate(notices, 1):
        created = datetime.datetime.fromisoformat(notice["created_at"].replace("Z", "+00:00"))
        notify_state.record_decision(state, notice["notice_id"], "deliver", created)
        batch_id = notify_state.add_pending_batch(
            state, notice["notice_id"], "Synthetic notice", created, format(index, "032x"))
        receipt = {"jobId": "synthetic-job", "action": "finished", "status": "ok",
                   "completionStatus": "succeeded", "runId": "synthetic-run-" + str(index),
                   "runAtMs": int(created.timestamp() * 1000), "durationMs": 1000,
                   "delivered": True, "deliveryStatus": "delivered",
                   "summary": "NOTIFY_BATCH=" + batch_id + "\nSynthetic notice"}
        notify_delivery.reconcile_state(
            state, [receipt], job_id="synthetic-job", now=created + datetime.timedelta(seconds=10))
        assert batch_id in state["acknowledgments"]
    return state


class QuietRoutingTests(unittest.TestCase):
    def test_activation_requires_fresh_dashboard_parity_and_both_acceptances(self):
        self.assertFalse(Q.activation_ready(
            revision=REVISION, dashboard_status="stale", parity_revision=REVISION,
            dashboard_working=True, acceptance=ACCEPTANCE))
        self.assertFalse(Q.activation_ready(
            revision=REVISION, dashboard_status="fresh", parity_revision=REVISION,
            dashboard_working=False, acceptance=ACCEPTANCE))
        self.assertFalse(Q.activation_ready(
            revision=REVISION, dashboard_status="fresh", parity_revision="b" * 40,
            dashboard_working=True, acceptance=ACCEPTANCE))
        self.assertFalse(Q.activation_ready(
            revision=REVISION, dashboard_status="fresh", parity_revision=REVISION,
            dashboard_working=True, acceptance={"phone": ACCEPTANCE["phone"]}))
        self.assertFalse(Q.activation_ready(
            revision=REVISION, dashboard_status="fresh", parity_revision=REVISION,
            dashboard_working=True,
            acceptance={**ACCEPTANCE, "desktop": dict(ACCEPTANCE["desktop"], revision="b" * 40)}))
        self.assertTrue(Q.activation_ready(
            revision=REVISION, dashboard_status="fresh", parity_revision=REVISION,
            dashboard_working=True, acceptance=ACCEPTANCE))

    def test_default_off_and_failed_gate_do_not_mutate_state(self):
        state = Q.empty_state()
        self.assertFalse(state["quiet_enabled"])
        next_state, result = Q.set_quiet_mode(
            state, True, cursor=CURSOR, revision=REVISION,
            dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION, acceptance=None)
        self.assertFalse(next_state["quiet_enabled"])
        self.assertFalse(result["changed"])
        self.assertEqual(state, Q.empty_state())

    def test_policy_dedup_restart_recovery_once_and_actionable_classes(self):
        state, enabled = Q.set_quiet_mode(
            Q.empty_state(), True, cursor=CURSOR, revision=REVISION,
            dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION, acceptance=ACCEPTANCE)
        self.assertTrue(enabled["changed"])
        updated_revision, update_result = Q.set_quiet_mode(
            state, True,
            cursor="2026-11-16T06:00:30Z", revision="b" * 40,
            dashboard_status="fresh", dashboard_working=True,
            parity_revision="b" * 40,
            acceptance={
                "phone": {"accepted": True, "revision": "b" * 40,
                          "recorded_at": "2026-11-16T06:00:20Z"},
                "desktop": {"accepted": True, "revision": "b" * 40,
                            "recorded_at": "2026-11-16T06:00:25Z"},
            })
        self.assertTrue(update_result["changed"])
        self.assertEqual(updated_revision["quiet_revision"], "b" * 40)
        self.assertEqual(updated_revision["history_before"], "2026-11-16T06:00:30Z")
        notices = [
            {"notice_id": "routine-1", "category": "progress", "created_at": "2026-11-16T06:01:00Z"},
            {"notice_id": "callback-1", "category": "callback", "created_at": "2026-11-16T06:02:00Z"},
            {"notice_id": "owner-1", "category": "owner_decision", "created_at": "2026-11-16T06:03:00Z"},
            {"notice_id": "failure-1", "category": "persistent_failure", "created_at": "2026-11-16T06:04:00Z"},
            {"notice_id": "recovery-1", "category": "recovery", "incident_id": "incident-1",
             "created_at": "2026-11-16T06:05:00Z"},
        ]
        state, planned = Q.plan_notices(
            state, notices, dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION)
        by_id = {item["notice_id"]: item for item in planned}
        self.assertEqual(by_id["routine-1"]["route"], "dashboard")
        for notice_id in ("callback-1", "owner-1", "failure-1", "recovery-1"):
            self.assertEqual(by_id[notice_id]["route"], "telegram")
        self.assertTrue(all(item["delivery_status"] == "not_attempted" for item in planned))

        restarted = json.loads(json.dumps(state))
        restarted, duplicate_results = Q.plan_notices(
            restarted, notices, dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION)
        self.assertTrue(all(item["reason"] == "pending_delivery" for item in duplicate_results))
        self.assertFalse(restarted["seen_notice_ids"])
        self.assertFalse(restarted["recovered_incidents"])
        receipts = confirmed_delivery(notices[1:])
        before = copy.deepcopy(receipts)
        restarted = Q.reconcile_deliveries(restarted, receipts)
        self.assertEqual(receipts, before)
        restarted, acknowledged = Q.plan_notices(restarted, notices)
        self.assertEqual(acknowledged[0]["reason"], "pending_delivery")
        self.assertTrue(all(item["route"] == "skipped" for item in acknowledged[1:]))
        restarted, duplicate_recovery = Q.plan_notices(
            restarted, [{"notice_id": "recovery-2", "category": "recovery",
                         "incident_id": "incident-1", "created_at": "2026-11-16T06:06:00Z"}],
            dashboard_status="fresh", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(duplicate_recovery[0]["reason"], "recovery_already_emitted")

    def test_unacknowledged_retries_survive_toggle_and_recovery_alias(self):
        callback = {"notice_id": "pending-callback", "category": "callback",
                    "created_at": "2026-11-16T06:01:00Z"}
        recovery = {"notice_id": "pending-recovery", "category": "recovery",
                    "incident_id": "incident-pending", "created_at": "2026-11-16T06:02:00Z"}
        policy, _ = Q.plan_notices(Q.empty_state(), [callback, recovery])
        unconfirmed = notify_state.empty_state()
        created = datetime.datetime.fromisoformat(callback["created_at"].replace("Z", "+00:00"))
        notify_state.record_decision(unconfirmed, callback["notice_id"], "deliver", created)
        notify_state.add_pending_batch(unconfirmed, callback["notice_id"], "Synthetic notice",
                                       created, "f" * 32)
        self.assertEqual(Q.reconcile_deliveries(policy, unconfirmed), policy)
        with self.assertRaises(Q.PolicyError):
            Q.reconcile_deliveries(policy, {"acknowledgments": {"fake": True}})
        policy, _ = Q.set_quiet_mode(
            policy, True, cursor="2026-11-16T06:03:00Z", revision=REVISION,
            dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION, acceptance=ACCEPTANCE)
        policy, retried = Q.plan_notices(json.loads(json.dumps(policy)), [
            callback, dict(recovery, notice_id="recovery-alias", created_at="2026-11-16T06:04:00Z")])
        self.assertEqual([row["notice_id"] for row in retried],
                         [callback["notice_id"], recovery["notice_id"]])
        self.assertTrue(all(row["reason"] == "pending_delivery" for row in retried))
        self.assertEqual(len(policy["pending_notices"]), 2)
        with self.assertRaises(Q.PolicyError):
            Q.plan_notices(policy, [dict(callback, category="progress")])

    def test_toggle_baselines_block_historical_replay_and_stale_dashboard_falls_back(self):
        initial = Q.empty_state()
        enabled, _ = Q.set_quiet_mode(
            initial, True, cursor=CURSOR, revision=REVISION,
            dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION, acceptance=ACCEPTANCE)
        enabled, old_results = Q.plan_notices(
            enabled, [{"notice_id": "old", "category": "progress", "created_at": CURSOR}],
            dashboard_status="fresh", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(old_results[0]["reason"], "historical_activation_baseline")
        enabled, active_results = Q.plan_notices(
            enabled, [{"notice_id": "new", "category": "progress", "created_at": "2026-11-16T06:01:00Z"}],
            dashboard_status="fresh", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(active_results[0]["route"], "dashboard")
        stale_results_state, stale_results = Q.plan_notices(
            enabled, [{"notice_id": "stale-dash", "category": "progress",
                       "created_at": "2026-11-16T06:02:00Z"}],
            dashboard_status="stale", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(stale_results[0]["route"], "telegram")
        self.assertEqual(stale_results[0]["reason"], "quiet_gate_failed")
        disabled, _ = Q.set_quiet_mode(stale_results_state, False,
                                       cursor="2026-11-16T06:03:00Z")
        disabled, historical = Q.plan_notices(
            disabled, [{"notice_id": "quiet-period", "category": "progress",
                        "created_at": "2026-11-16T06:02:30Z"}],
            dashboard_status="fresh", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(historical[0]["reason"], "historical_activation_baseline")
        _, current = Q.plan_notices(
            disabled, [{"notice_id": "after-off", "category": "progress",
                        "created_at": "2026-11-16T06:04:00Z"}],
            dashboard_status="fresh", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(current[0]["route"], "telegram")

    def test_pending_delivery_state_survives_policy_and_generation_is_not_ack(self):
        delivery = notify_state.empty_state()
        created_at = datetime.datetime(2026, 11, 16, 6, 0,
                                       tzinfo=datetime.timezone.utc)
        notify_state.record_decision(delivery, "decision-1", "deliver", created_at)
        batch_id = notify_state.add_pending_batch(
            delivery, "decision-1", "Synthetic pending notice", created_at, "a" * 32)
        original_delivery = copy.deepcopy(delivery)
        policy, _ = Q.set_quiet_mode(
            Q.empty_state(), True, cursor=CURSOR, revision=REVISION,
            dashboard_status="fresh", dashboard_working=True,
            parity_revision=REVISION, acceptance=ACCEPTANCE)
        policy, planned = Q.plan_notices(
            policy, [{"notice_id": "new-generated", "category": "digest",
                      "created_at": "2026-11-16T06:01:00Z"}],
            dashboard_status="fresh", dashboard_working=True, parity_revision=REVISION)
        self.assertEqual(planned[0]["delivery_status"], "not_attempted")
        self.assertEqual(delivery, original_delivery)
        self.assertIn(batch_id, delivery["pending_batches"])
        self.assertNotIn(batch_id, delivery["acknowledgments"])


if __name__ == "__main__":
    unittest.main()
