"""Offline checks for the OpenClaw queue runner and controlled fake portal."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import itertools
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import openclaw_drain as od
import openclaw_ownership as ownership


RUNNER = "runner-a"
SESSION = "agent:main:jobs-runner-runner-a-00000000000000000000000000000001"


class FakeQueue:
    """Fake queue with a single selector and durable owner semantics."""

    def __init__(self, rows=None, verifier=None):
        self.rows = rows or [{"pr_number": 11, "rank": 5, "status": "queued"}]
        self.owner = None
        self.outcomes = {}
        self.calls = []
        self.verifier = verifier
        self.fail_finish = False
        self.fail_after_release = False
        self.claim_override = None

    def select_next(self):
        return next((row for row in self.rows if row["status"] == "queued"), None)

    def browser_owner(self):
        self.calls.append("browser_owner")
        return dict(self.owner) if self.owner else None

    def claim_next(self, runner_id, session_ref):
        self.calls.append(("claim", runner_id, session_ref))
        if self.owner:
            raise RuntimeError("slot occupied")
        row = self.select_next()
        if row is None:
            return None
        row["status"] = "in_progress"
        self.owner = {
            "pr_number": row["pr_number"],
            "attempt_id": "att-%s" % row["pr_number"],
            "runner_id": runner_id,
            "session_ref": session_ref,
            "legacy": False,
        }
        if self.claim_override:
            return self.claim_override(dict(self.owner))
        return dict(self.owner)

    def record_outcome(self, pr_number, outcome):
        self.outcomes[pr_number] = outcome

    def finish_attempt(self, pr_number, attempt_id, runner_id, session_ref, evidence):
        self.calls.append(("finish", pr_number, evidence))
        owner = self.owner
        if not owner or (owner["pr_number"], owner["attempt_id"], owner["runner_id"],
                         owner["session_ref"]) != (pr_number, attempt_id, runner_id, session_ref):
            raise RuntimeError("stale owner")
        if self.fail_finish:
            raise RuntimeError("verifier unavailable")
        if self.verifier:
            request = {
                "schema": ownership.REQUEST_SCHEMA,
                "challenge": "a" * 32,
                "owner": dict(owner),
                "termination_evidence": evidence,
            }
            verdict = self.verifier(request)
            if verdict.get("verified") is not True:
                raise RuntimeError("verifier did not confirm")
        outcome = self.outcomes.get(pr_number, "outcome_unknown")
        row = next(row for row in self.rows if row["pr_number"] == pr_number)
        row["status"] = outcome
        self.owner = None
        result = {
            "pr_number": pr_number,
            "attempt_id": attempt_id,
            "runner_id": runner_id,
            "session_ref": session_ref,
            "outcome": outcome,
            "browser_owner_released": True,
        }
        if self.fail_after_release:
            raise RuntimeError("lost finish response")
        return result


class FakeAgent:
    def __init__(self, queue, outcome="submitted", result=None, error=None):
        self.queue, self.outcome = queue, outcome
        self.result, self.error = result, error
        self.requests = []

    def start_turn(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.outcome:
            self.queue.record_outcome(request.pr_number, self.outcome)
        return self.result or od.TurnResult(True, run_id="oc-run-1", return_code=0)


class FakePortal:
    """A portal with pending work that can be rejected at its session boundary."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fenced = set()
        self.actions = []
        self.rejected = []
        self.pending = {}

    def start_pending_action(self, session_ref):
        release = threading.Event()
        started = threading.Event()

        def action():
            started.set()
            release.wait(5)
            with self.lock:
                if session_ref in self.fenced:
                    self.rejected.append((session_ref, "submit"))
                else:
                    self.actions.append((session_ref, "submit"))

        thread = threading.Thread(target=action, daemon=True)
        self.pending[session_ref] = (thread, release, started)
        thread.start()
        assert started.wait(2)

    def fence(self, session_ref):
        with self.lock:
            self.fenced.add(session_ref)
        pending = self.pending.get(session_ref)
        if pending:
            thread, release, _ = pending
            release.set()
            thread.join(2)
            assert not thread.is_alive()

    def act(self, session_ref, action):
        with self.lock:
            if session_ref in self.fenced:
                self.rejected.append((session_ref, action))
                return False
            self.actions.append((session_ref, action))
            return True


class FakeGateway:
    def __init__(self, portal):
        self.portal = portal
        self.active = set()
        self.calls = []

    def start(self, session_ref):
        self.active.add(session_ref)

    def call(self, method, params):
        assert method == "sessions.abort"
        assert params.get("clearQueued") is True
        session_ref = params["key"]
        self.calls.append((method, dict(params)))
        if session_ref not in self.active:
            return {"status": "no-active-run"}
        self.portal.fence(session_ref)
        self.active.remove(session_ref)
        return {"status": "aborted"}


def test_queue_client_uses_the_shared_claim_next_selector():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="null", stderr="ignored")

    client = od.SubprocessQueueClient("queue.py", python="python", run=fake_run)
    assert client.claim_next("runner-a", SESSION) is None
    assert calls[0][0] == ["python", "queue.py", "claim", "--next", "--runner-id",
                           "runner-a", "--session-ref", SESSION, "--json"]
    assert calls[0][1].get("shell") is not True


def test_openclaw_adapter_uses_exact_session_and_owner_tuple():
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["prompt"] = open(argv[argv.index("--message-file") + 1], encoding="utf-8").read()
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout='{"status":"ok","runId":"run-9"}')

    adapter = od.OpenClawAgentAdapter("/opt/openclaw", "main", "scripts/queue.py",
                                      run=fake_run)
    req = od.TurnRequest(RUNNER, SESSION, 23, "attempt-23")
    result = adapter.start_turn(req)
    assert result == od.TurnResult(True, run_id="run-9", return_code=0)
    argv = observed["argv"]
    assert argv[:4] == ["/opt/openclaw", "agent", "--agent", "main"]
    assert argv[argv.index("--session-key") + 1] == SESSION
    assert argv[argv.index("--message-file") + 1] != "-"
    assert "attempt_id: attempt-23" in observed["prompt"]
    assert "runner_id: runner-a" in observed["prompt"]
    assert "session_ref: " + SESSION in observed["prompt"]
    assert os.path.join("skills", "job-queue-drain", "SKILL.md") in observed["prompt"]
    assert observed["kwargs"]["capture_output"] is True
    assert observed["kwargs"]["check"] is False


def test_openclaw_version_gate_is_exact_and_read_only():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="OpenClaw 2026.10.1 (e178c39)\n")

    assert od.check_openclaw_version("/opt/openclaw", run)
    assert calls[0][0] == ["/opt/openclaw", "--version"]
    assert calls[0][1]["timeout"] == 5
    assert not od.check_openclaw_version("/opt/openclaw", lambda *args, **kwargs:
                                         SimpleNamespace(returncode=0, stdout="OpenClaw newer"))


def test_run_cli_wires_claim_agent_and_finish_path():
    queue = FakeQueue()
    agent = FakeAgent(queue, "submitted")
    output = io.StringIO()
    with mock.patch.object(od, "check_openclaw_version", return_value=True), \
            mock.patch.object(od, "SubprocessQueueClient", return_value=queue), \
            mock.patch.object(od, "OpenClawAgentAdapter", return_value=agent), \
            contextlib.redirect_stdout(output):
        code = od.main(["run", "--runner-id", RUNNER, "--max-attempts", "1"])
    assert code == 0
    report = json.loads(output.getvalue())
    assert report["attempts"][0]["persisted_outcome"] == "submitted"
    assert any(isinstance(call, tuple) and call[0] == "claim" for call in queue.calls)
    assert any(isinstance(call, tuple) and call[0] == "finish" for call in queue.calls)


def test_agent_timeout_is_not_an_outcome_and_still_finalizes():
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    queue = FakeQueue()
    agent = od.OpenClawAgentAdapter("openclaw", "main", "queue.py", run=timeout)
    runner = od.DrainRunner(od.DrainConfig(RUNNER, max_attempts=1), queue, agent,
                           session_factory=lambda _: SESSION)
    report = runner.run()
    assert report.status == "uncertain"
    assert report.attempts[0].timed_out
    assert report.attempts[0].persisted_outcome == "outcome_unknown"
    assert report.attempts[0].owner_released
    finish = next(call for call in queue.calls if isinstance(call, tuple) and call[0] == "finish")
    assert finish[2]["client_timed_out"] is True


def test_invalid_adapter_result_still_uses_independent_finish_path():
    queue = FakeQueue()
    agent = FakeAgent(queue, outcome=None, result=object())
    runner = od.DrainRunner(od.DrainConfig(RUNNER), queue, agent,
                           session_factory=lambda _: SESSION)
    report = runner.run()
    assert report.status == "uncertain"
    assert report.attempts[0].turn_error == "invalid_adapter_result"
    assert report.attempts[0].owner_released
    assert report.attempts[0].persisted_outcome == "outcome_unknown"


def test_known_outcomes_resolve_after_verified_owner_release():
    for outcome in ("blocked", "failed", "skipped", "submitted", "retry"):
        queue = FakeQueue()
        agent = FakeAgent(queue, outcome)
        runner = od.DrainRunner(od.DrainConfig(RUNNER), queue, agent,
                               session_factory=lambda _: SESSION)
        report = runner.run()
        assert report.attempts[0].persisted_outcome == outcome
        assert report.attempts[0].owner_released
        assert report.attempts[0].resolved
        assert report.status == "limit_reached"


def test_outcome_unknown_releases_slot_then_allows_next_distinct_app():
    rows = [{"pr_number": 4, "rank": 10, "status": "queued"},
            {"pr_number": 5, "rank": 9, "status": "queued"}]
    queue = FakeQueue(rows)
    class TwoOutcomeAgent(FakeAgent):
        def start_turn(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                self.queue.record_outcome(request.pr_number, "submitted")
            return od.TurnResult(True, run_id="run-%d" % len(self.requests), return_code=0)

    agent = TwoOutcomeAgent(queue, outcome=None)
    refs = iter(SESSION + "-next-%d" % number for number in itertools.count())
    runner = od.DrainRunner(od.DrainConfig(RUNNER, max_attempts=2), queue, agent,
                           session_factory=lambda _: next(refs))
    report = runner.run()
    assert [attempt.pr_number for attempt in report.attempts] == [4, 5]
    assert report.attempts[0].persisted_outcome == "outcome_unknown"
    assert not report.attempts[0].resolved
    assert report.attempts[1].resolved
    assert report.status == "uncertain"
    assert [call[1] for call in queue.calls if isinstance(call, tuple) and call[0] == "claim"] == [
        "runner-a", "runner-a"]


def test_existing_owner_is_finished_before_a_new_claim():
    queue = FakeQueue()
    queue.owner = {"pr_number": 1, "attempt_id": "old-att", "runner_id": RUNNER,
                   "session_ref": SESSION, "legacy": False}
    queue.rows = [{"pr_number": 1, "rank": 1, "status": "outcome_unknown"},
                  {"pr_number": 2, "rank": 2, "status": "queued"}]
    queue.outcomes[1] = "outcome_unknown"
    agent = FakeAgent(queue, "submitted")
    runner = od.DrainRunner(od.DrainConfig(RUNNER), queue, agent,
                           session_factory=lambda _: SESSION + "-next")
    report = runner.run()
    finish_index = next(i for i, call in enumerate(queue.calls)
                        if isinstance(call, tuple) and call[0] == "finish")
    claim_index = next(i for i, call in enumerate(queue.calls)
                       if isinstance(call, tuple) and call[0] == "claim")
    assert finish_index < claim_index
    assert report.attempts[0].recovered and report.attempts[0].owner_released
    assert agent.requests[0].pr_number == 2


def test_verifier_failure_keeps_owner_and_prevents_new_claim():
    queue = FakeQueue()
    queue.fail_finish = True
    agent = FakeAgent(queue)
    runner = od.DrainRunner(od.DrainConfig(RUNNER), queue, agent,
                           session_factory=lambda _: SESSION)
    report = runner.run()
    assert report.status == "owner_preserved"
    assert queue.owner is not None
    assert sum(isinstance(call, tuple) and call[0] == "claim" for call in queue.calls) == 1
    assert len(agent.requests) == 1


def test_lost_finish_response_can_continue_only_after_owner_readback_is_free():
    queue = FakeQueue(rows=[{"pr_number": 8, "rank": 2, "status": "queued"},
                            {"pr_number": 9, "rank": 1, "status": "queued"}])
    queue.fail_after_release = True
    # The response is lost after the first independently verified release.
    refs = iter(SESSION + "-next-%d" % number for number in itertools.count())
    runner = od.DrainRunner(od.DrainConfig(RUNNER, max_attempts=2), queue,
                            FakeAgent(queue, outcome=None),
                            session_factory=lambda _: next(refs))
    report = runner.run()
    assert [item.pr_number for item in report.attempts] == [8, 9]
    assert all(item.owner_released for item in report.attempts)


def test_fake_portal_rejects_outstanding_old_action_before_slot_release():
    portal = FakePortal()
    gateway = FakeGateway(portal)

    class PortalAgent:
        def __init__(self, queue):
            self.queue = queue

        def start_turn(self, request):
            gateway.start(request.session_ref)
            portal.start_pending_action(request.session_ref)
            # The client exits while the server-side action is still outstanding.
            self.queue.record_outcome(request.pr_number, "outcome_unknown")
            return od.TurnResult(False, run_id="run-pending", return_code=1)

    queue = FakeQueue(verifier=lambda request: ownership.verify_request(request, gateway.call))
    runner = od.DrainRunner(od.DrainConfig(RUNNER), queue, PortalAgent(queue),
                           session_factory=lambda _: SESSION)
    report = runner.run()
    assert report.status == "uncertain"
    assert queue.owner is None
    assert gateway.calls[0][0] == "sessions.abort"
    assert gateway.calls[1][0] == "sessions.abort"
    assert not portal.actions
    assert portal.rejected == [(SESSION, "submit")]
    assert portal.act(SESSION, "late-submit") is False
    assert (SESSION, "late-submit") in portal.rejected


def test_claim_tuple_mismatch_stops_before_agent_turn():
    queue = FakeQueue()
    queue.claim_override = lambda owner: dict(owner, session_ref="agent:main:other")
    agent = FakeAgent(queue)
    runner = od.DrainRunner(od.DrainConfig(RUNNER), queue, agent,
                           session_factory=lambda _: SESSION)
    report = runner.run()
    assert report.status == "claim_invalid"
    assert not agent.requests


def test_subprocess_queue_passes_verifier_evidence_as_json():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"outcome":"submitted"}')

    client = od.SubprocessQueueClient("queue.py", "python", run=fake_run)
    evidence = {"schema": od.RUNNER_EVIDENCE_SCHEMA, "session_ref": SESSION}
    client.finish_attempt(3, "att-3", RUNNER, SESSION, evidence)
    args = calls[0]
    raw = args[args.index("--termination-evidence") + 1]
    assert json.loads(raw) == evidence
    assert args[-1] == "--json"


def installed_job(**changes):
    job = {"id": "job-1", "schedule": {"kind": "cron", "expr": "0 9 * * *",
                                          "tz": "Asia/Singapore"},
           "payload": {"kind": "agentTurn", "message": "synthetic payload"},
           "enabled": False, "agentId": "main", "sessionTarget": "isolated",
           "delivery": {"mode": "none"}}
    job.update(changes)
    return job


def test_readback_detects_payload_trigger_and_enabled_drift():
    intended = installed_job()
    expected = od.expected_digests(intended)
    assert od.verify_scheduler_readback({"jobs": [intended], "hasMore": False},
                                        "job-1", expected).ok
    changed = installed_job(payload={"kind": "agentTurn", "message": "changed"})
    result = od.verify_scheduler_readback({"jobs": [changed]}, "job-1", expected)
    assert result.mismatches == ("payload:digest_mismatch",)
    assert "synthetic payload" not in repr(result)
    changed = installed_job(enabled=True, schedule={"kind": "cron", "expr": "0 * * * *"})
    result = od.verify_scheduler_readback({"jobs": [changed]}, "job-1", expected)
    assert result.mismatches == ("enabled:digest_mismatch", "schedule:digest_mismatch")


def test_readback_fails_closed_on_incomplete_or_ambiguous_listing():
    expected = od.expected_digests(installed_job())
    verify = od.verify_scheduler_readback
    assert verify({"jobs": [installed_job()], "hasMore": True}, "job-1", expected).mismatches == (
        "listing:incomplete",)
    assert verify({"jobs": []}, "job-1", expected).mismatches == ("job:expected_one_match",)
    assert verify({"jobs": [installed_job(), installed_job()]}, "job-1", expected).mismatches == (
        "job:expected_one_match",)
    assert verify(None, "job-1", expected).mismatches == ("listing:incomplete",)
    partial = {key: value for key, value in expected.items() if key != "payload"}
    assert verify({"jobs": [installed_job()]}, "job-1", partial).mismatches == (
        "payload:no_expected_digest",)


def test_readback_cli_prints_only_safe_mismatch_codes():
    job = installed_job()
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "expected.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(od.expected_digests(job), stream)
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"jobs": [job]}))), \
                contextlib.redirect_stdout(output):
            assert od.main(["verify-readback", "--job-id", "job-1", "--expected", path]) == 0
        assert json.loads(output.getvalue())["ok"] is True
        assert "synthetic payload" not in output.getvalue()


def test_config_digest_matches_canonical_json():
    import hashlib
    value = {"b": [1, "é"], "a": None}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert od.config_digest(value) == hashlib.sha256(raw).hexdigest()


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
    print("test_openclaw_drain: %d checks passed" % len(TESTS))
