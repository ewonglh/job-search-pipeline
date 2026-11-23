"""Focused checks for the claim-pinned OpenClaw session verifier."""

import json
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import openclaw_ownership as verifier


SESSION = "agent:main:jobs-runner-runner-a-00000000000000000000000000000001"


def request(session_ref=SESSION):
    owner = {"pr_number": 17, "attempt_id": "attempt-17", "runner_id": "runner-a",
             "session_ref": session_ref, "legacy": False}
    evidence = {"schema": verifier.EVIDENCE_SCHEMA, "pr_number": 17,
                "attempt_id": "attempt-17", "runner_id": "runner-a",
                "session_ref": session_ref, "source": "turn",
                "client_exit_ok": False, "client_timed_out": True}
    return {"schema": verifier.REQUEST_SCHEMA, "challenge": "b" * 32,
            "owner": owner, "termination_evidence": evidence}


def test_verifier_aborts_exact_session_and_requires_no_active_readback():
    calls = []
    replies = iter(({"status": "aborted"}, {"status": "no-active-run"}))

    def rpc(method, params):
        calls.append((method, dict(params)))
        return next(replies)

    result = verifier.verify_request(request(), rpc)
    assert len(calls) == 2
    assert all(method == "sessions.abort" for method, _ in calls)
    assert all(params == {"key": SESSION, "agentId": "main", "clearQueued": True}
               for _, params in calls)
    assert result["verified"] is True
    assert result["schema"] == verifier.VERDICT_SCHEMA
    assert result["state"] == "terminated"
    assert result["challenge"] == "b" * 32
    assert result["session_ref"] == SESSION
    assert result["verifier_id"] == verifier.VERIFIER_ID


def test_already_terminal_session_needs_two_no_active_readbacks():
    statuses = iter(("no-active-run", "no-active-run"))
    assert verifier.verify_request(request(), lambda *args: {"status": next(statuses)})[
        "verified"]


def test_verifier_fails_closed_if_abort_or_readback_is_incomplete():
    for statuses in (("active", "no-active-run"), ("aborted", "aborted")):
        calls = []

        def rpc(method, params):
            calls.append(method)
            return {"status": statuses[len(calls) - 1]}

        try:
            verifier.verify_request(request(), rpc)
        except verifier.VerificationError:
            pass
        else:
            raise AssertionError("accepted a non-terminal session")
        assert len(calls) == (1 if statuses[0] == "active" else 2)


def test_owner_evidence_mismatch_and_legacy_session_are_rejected_before_rpc():
    malformed = request()
    malformed["termination_evidence"]["runner_id"] = "other-runner"
    calls = []
    try:
        verifier.verify_request(malformed, lambda *args: calls.append(args))
    except verifier.VerificationError:
        pass
    else:
        raise AssertionError("accepted mismatched owner evidence")
    assert not calls

    for session_ref in ("legacy-unbound", "agent:main:other-session"):
        try:
            verifier.verify_request(request(session_ref), lambda *args: calls.append(args))
        except verifier.VerificationError:
            pass
        else:
            raise AssertionError("accepted a session outside the one-use runner namespace")
    assert not calls


def test_gateway_cli_call_uses_sessions_abort_and_parses_response_envelope():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0,
                               stdout=json.dumps({"ok": True,
                                                  "payload": {"status": "no-active-run"}}),
                               stderr="private output")

    binary = os.path.abspath(os.path.join("opt", "openclaw"))
    result = verifier._gateway_call(binary, "sessions.abort",
                                    {"key": SESSION, "agentId": "main",
                                     "clearQueued": True}, run=run)
    assert result == {"status": "no-active-run"}
    argv, kwargs = calls[0]
    assert argv[:4] == [binary, "gateway", "call", "sessions.abort"]
    assert json.loads(argv[argv.index("--params") + 1])["key"] == SESSION
    assert kwargs["check"] is False and "shell" not in kwargs
    assert "private output" not in repr(result)


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
    print("test_openclaw_ownership: %d checks passed" % len(TESTS))
