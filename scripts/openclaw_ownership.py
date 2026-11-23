#!/usr/bin/env python3
"""Queue verifier using OpenClaw's session-scoped abort RPC."""

import json
import os
import re
import subprocess
import sys
import argparse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


REQUEST_SCHEMA = "job-search-browser-termination-request/v1"
EVIDENCE_SCHEMA = "job-search-openclaw-session-evidence/v1"
VERDICT_SCHEMA = "job-search-browser-termination-verdict/v1"
VERIFIER_ID = "openclaw-sessions-abort-v1"
OWNER_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
CHALLENGE_RE = re.compile(r"^[A-Fa-f0-9]{32,128}$")
INPUT_MAX_BYTES = 16384
RPC_TIMEOUT_MS = 7000
SUPPORTED_OPENCLAW_VERSION = "OpenClaw 2026.10.1 (e178c39)"


class VerificationError(ValueError):
    """The OpenClaw session could not be independently fenced."""


def _session_agent(session_ref: str) -> str:
    parts = session_ref.split(":", 2)
    if (len(parts) != 3 or parts[0] != "agent"
            or not AGENT_ID_RE.fullmatch(parts[1])
            or not parts[2].startswith("jobs-runner-")):
        raise VerificationError("session is not a one-use runner session")
    return parts[1]


def _validated_request(request: Any) -> Dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
        raise VerificationError("request schema mismatch")
    challenge = request.get("challenge")
    if not isinstance(challenge, str) or not CHALLENGE_RE.fullmatch(challenge):
        raise VerificationError("challenge is malformed")
    owner = request.get("owner")
    evidence = request.get("termination_evidence")
    if not isinstance(owner, dict) or not isinstance(evidence, dict):
        raise VerificationError("owner or evidence is malformed")
    pr_number = owner.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise VerificationError("owner PR is malformed")
    fields = ("attempt_id", "runner_id", "session_ref")
    values = {key: owner.get(key) for key in fields}
    if not all(isinstance(value, str) and OWNER_ID_RE.fullmatch(value)
               for value in values.values()):
        raise VerificationError("owner tuple is malformed")
    if owner.get("legacy") is True:
        raise VerificationError("legacy owner has no verifiable OpenClaw session")
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise VerificationError("runner evidence schema mismatch")
    if evidence.get("pr_number") != pr_number or any(
            evidence.get(key) != owner.get(key) for key in fields):
        raise VerificationError("runner evidence owner mismatch")
    if evidence.get("source") not in ("turn", "recovery"):
        raise VerificationError("runner evidence source is invalid")
    if evidence.get("source") == "turn":
        if not isinstance(evidence.get("client_exit_ok"), bool) \
                or not isinstance(evidence.get("client_timed_out"), bool):
            raise VerificationError("runner turn result is malformed")
    session_ref = values["session_ref"]
    agent_id = _session_agent(session_ref)
    return {
        "challenge": challenge,
        "owner": {"pr_number": pr_number, **values},
        "agent_id": agent_id,
    }


def verify_request(request: Any,
                   rpc_call: Callable[[str, Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Abort the exact one-use session and require a terminal second read-back.

    The first RPC cancels active work and clears queued session follow-ups. The
    Gateway waits for its terminal lifecycle persistence before responding. A
    second independent read must report no active run before the queue can
    release the browser owner.
    """
    checked = _validated_request(request)
    session_ref, agent_id = checked["owner"]["session_ref"], checked["agent_id"]
    params = {"key": session_ref, "agentId": agent_id, "clearQueued": True}
    first = rpc_call("sessions.abort", params)
    if not isinstance(first, dict):
        raise VerificationError("first session abort returned an invalid result")
    first_status = first.get("status")
    if first_status not in ("aborted", "no-active-run"):
        raise VerificationError("first session abort did not establish a stop")
    if isinstance(first.get("warning"), str) and first["warning"]:
        raise VerificationError("session abort returned a warning")

    second = rpc_call("sessions.abort", params)
    if not isinstance(second, dict):
        raise VerificationError("terminal read-back returned an invalid result")
    second_status = second.get("status")
    if second_status != "no-active-run":
        raise VerificationError("session is still active after abort")
    if isinstance(second.get("warning"), str) and second["warning"]:
        raise VerificationError("terminal read-back returned a warning")

    owner = checked["owner"]
    return {
        "schema": VERDICT_SCHEMA,
        "verified": True,
        "challenge": checked["challenge"],
        "pr_number": owner["pr_number"],
        "attempt_id": owner["attempt_id"],
        "runner_id": owner["runner_id"],
        "session_ref": session_ref,
        "verifier_id": VERIFIER_ID,
        "state": "terminated",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence": "sessions.abort then no-active-run read-back",
    }


def _unwrap_abort_result(value: Any) -> Optional[Dict[str, Any]]:
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return None
        if current.get("status") in ("aborted", "no-active-run"):
            return current
        current = next((current.get(key) for key in ("payload", "result", "data")
                        if isinstance(current.get(key), dict)), None)
    return None


def _gateway_call(binary: str, method: str, params: Dict[str, Any],
                  run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    if (not isinstance(binary, str) or not binary or not os.path.isabs(binary)
            or method != "sessions.abort"):
        raise VerificationError("unsupported Gateway method")
    argv = [binary, "gateway", "call", method, "--json", "--timeout",
            str(RPC_TIMEOUT_MS), "--params",
            json.dumps(params, sort_keys=True, separators=(",", ":"))]
    try:
        proc = run(argv, capture_output=True, text=True,
                   timeout=RPC_TIMEOUT_MS / 1000 + 3, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError("Gateway RPC did not complete: " + type(exc).__name__) from None
    if proc.returncode != 0:
        raise VerificationError("Gateway RPC failed")
    try:
        decoded = json.loads(proc.stdout)
    except (TypeError, ValueError):
        raise VerificationError("Gateway RPC returned invalid JSON") from None
    result = _unwrap_abort_result(decoded)
    if result is None:
        raise VerificationError("Gateway RPC omitted terminal status")
    return result


def _check_version(binary: str) -> None:
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError("OpenClaw version check failed: " + type(exc).__name__) from None
    if proc.returncode != 0 or proc.stdout.strip() != SUPPORTED_OPENCLAW_VERSION:
        raise VerificationError("OpenClaw version is unsupported")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--openclaw-bin", required=True,
                        help="absolute path to the pinned OpenClaw CLI")
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(INPUT_MAX_BYTES + 1)
    if len(raw) > INPUT_MAX_BYTES:
        print("openclaw-ownership: request_too_large", file=sys.stderr)
        return 2
    try:
        request = json.loads(raw.decode("utf-8"))
        binary = args.openclaw_bin
        if not os.path.isabs(binary):
            raise VerificationError("OpenClaw path must be absolute")
        _check_version(binary)
        verdict = verify_request(
            request, lambda method, params: _gateway_call(binary, method, params))
    except (UnicodeError, ValueError, OSError):
        print("openclaw-ownership: verification_failed", file=sys.stderr)
        return 1
    print(json.dumps(verdict, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
