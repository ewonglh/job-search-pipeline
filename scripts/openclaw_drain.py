#!/usr/bin/env python3
"""Claim queue work, run one isolated OpenClaw session, and verify release."""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple


RUNNER_EVIDENCE_SCHEMA = "job-search-openclaw-session-evidence/v1"
RESOLVED_OUTCOMES = frozenset({"blocked", "failed", "skipped", "submitted", "retry"})
QUEUE_OUTCOMES = RESOLVED_OUTCOMES | {"outcome_unknown"}
OWNER_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
OK_RUN_STATUSES = frozenset({"idle", "limit_reached"})
SUPPORTED_OPENCLAW_VERSION = "OpenClaw 2026.10.1 (e178c39)"


class ConfigError(ValueError):
    """Invalid runner configuration."""


class QueueError(RuntimeError):
    """Queue CLI failed or returned malformed JSON."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def config_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class TurnRequest:
    runner_id: str
    session_ref: str
    pr_number: int
    attempt_id: str


@dataclass(frozen=True)
class TurnResult:
    exit_ok: bool
    run_id: Optional[str] = None
    timed_out: bool = False
    return_code: Optional[int] = None


class AgentAdapter(Protocol):
    def start_turn(self, request: TurnRequest) -> TurnResult: ...


class QueueClient(Protocol):
    def browser_owner(self) -> Optional[Dict[str, Any]]: ...

    def claim_next(self, runner_id: str, session_ref: str) -> Optional[Dict[str, Any]]: ...

    def finish_attempt(self, pr_number: int, attempt_id: str, runner_id: str,
                       session_ref: str, termination_evidence: Dict[str, Any]) -> Dict[str, Any]: ...


class SubprocessQueueClient:
    """Use queue.py as the sole selector and durable ownership authority."""

    def __init__(self, queue_script: str, python: str = sys.executable,
                 timeout: float = 120.0,
                 run: Callable[..., Any] = subprocess.run):
        self._base = [python, queue_script]
        self._timeout = timeout
        self._run = run

    def _call(self, args: List[str]) -> Any:
        try:
            proc = self._run(self._base + args, capture_output=True, text=True,
                             timeout=self._timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise QueueError("queue command did not run: " + type(exc).__name__) from None
        if proc.returncode != 0:
            raise QueueError("queue command exited %s" % proc.returncode)
        try:
            return json.loads(proc.stdout)
        except (TypeError, ValueError):
            raise QueueError("queue command returned invalid JSON") from None

    def browser_owner(self) -> Optional[Dict[str, Any]]:
        return self._call(["browser-owner", "--json"])

    def claim_next(self, runner_id: str, session_ref: str) -> Optional[Dict[str, Any]]:
        # Ordering belongs to the locked queue selector shared by all callers.
        return self._call(["claim", "--next", "--runner-id", runner_id,
                           "--session-ref", session_ref, "--json"])

    def finish_attempt(self, pr_number: int, attempt_id: str, runner_id: str,
                       session_ref: str, termination_evidence: Dict[str, Any]) -> Dict[str, Any]:
        return self._call(["finish-attempt", "--pr", str(pr_number),
                           "--attempt-id", attempt_id, "--runner-id", runner_id,
                           "--session-ref", session_ref,
                           "--termination-evidence",
                           canonical_json(termination_evidence).decode("utf-8"), "--json"])


def _extract_run_id(value: Any) -> Optional[str]:
    """Read only a run identifier from the CLI envelope; never trust its prose."""
    if isinstance(value, dict):
        for key in ("runId", "run_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate[:128]
        for child in value.values():
            found = _extract_run_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_run_id(child)
            if found:
                return found
    return None


def _agent_prompt(request: TurnRequest, queue_script: str) -> str:
    script_path = os.path.abspath(queue_script)
    repo_root = os.path.dirname(os.path.dirname(script_path))
    skill_path = os.path.join(repo_root, "skills", "job-queue-drain", "SKILL.md")
    return "\n".join((
        "Handle exactly one already-authorized application queue attempt.",
        "Do not claim another item or change the owner tuple.",
        "Follow the queue instructions at the absolute path below and use its existing queue.py commands.",
        "Keep the merge approval, owner skip/cap, eligibility, answer, and snapshot gates intact.",
        "Stop at any captcha, ambiguous required answer, or uncertain submission state.",
        "Persist only an evidence-backed outcome using the exact attempt and owner IDs below.",
        "Agent prose is not an outcome; the queue's finish-attempt result is authoritative.",
        "",
        "repository root: " + repo_root,
        "queue instructions: " + skill_path,
        "queue.py: " + script_path,
        "PR number: %d" % request.pr_number,
        "attempt_id: " + request.attempt_id,
        "runner_id: " + request.runner_id,
        "session_ref: " + request.session_ref,
    ))


class OpenClawAgentAdapter:
    """Run a real Gateway-backed OpenClaw turn in the claimed unique session."""

    def __init__(self, binary: str, agent_id: str, queue_script: str,
                 timeout: float = 600.0,
                 run: Callable[..., Any] = subprocess.run):
        if not binary or not isinstance(binary, str):
            raise ConfigError("OpenClaw binary must be configured")
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise ConfigError("OpenClaw agent ID is malformed")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 1):
            raise ConfigError("agent timeout must be at least one second")
        self.binary, self.agent_id = binary, agent_id
        self.queue_script, self.timeout, self._run = queue_script, float(timeout), run

    def start_turn(self, request: TurnRequest) -> TurnResult:
        prompt = _agent_prompt(request, self.queue_script)
        try:
            with tempfile.TemporaryDirectory(prefix="job-search-openclaw-") as temp:
                prompt_path = os.path.join(temp, "attempt.txt")
                with open(prompt_path, "w", encoding="utf-8") as stream:
                    stream.write(prompt)
                try:
                    os.chmod(prompt_path, 0o600)
                except OSError:
                    pass
                argv = [self.binary, "agent", "--agent", self.agent_id,
                        "--session-key", request.session_ref, "--message-file", prompt_path,
                        "--json", "--timeout", str(max(1, int(self.timeout)))]
                try:
                    proc = self._run(argv, capture_output=True, text=True,
                                     timeout=self.timeout + 15, check=False)
                except subprocess.TimeoutExpired:
                    # Killing this CLI is not termination evidence. finish-attempt
                    # invokes the claim-pinned session verifier independently.
                    return TurnResult(False, timed_out=True)
                except (OSError, subprocess.SubprocessError):
                    return TurnResult(False)
        except OSError:
            return TurnResult(False)
        try:
            envelope = json.loads(proc.stdout)
        except (TypeError, ValueError):
            envelope = None
        return TurnResult(proc.returncode == 0, _extract_run_id(envelope),
                          return_code=proc.returncode)


@dataclass(frozen=True)
class Binding:
    pr_number: int
    attempt_id: str
    runner_id: str
    session_ref: str


def parse_binding(value: Any) -> Optional[Binding]:
    if not isinstance(value, dict):
        return None
    pr_number = value.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return None
    fields = [value.get(key) for key in ("attempt_id", "runner_id", "session_ref")]
    if not all(isinstance(field, str) and OWNER_ID_RE.fullmatch(field) for field in fields):
        return None
    return Binding(pr_number, *fields)


@dataclass
class AttemptReport:
    pr_number: int
    attempt_id: str
    recovered: bool = False
    turn_ok: Optional[bool] = None
    turn_error: Optional[str] = None
    timed_out: bool = False
    persisted_outcome: Optional[str] = None
    owner_released: bool = False
    resolved: bool = False


@dataclass
class RunReport:
    status: str
    attempts: List[AttemptReport] = field(default_factory=list)
    detail: Optional[str] = None

    @property
    def unresolved(self) -> List[AttemptReport]:
        return [attempt for attempt in self.attempts if not attempt.resolved]

    @property
    def ok(self) -> bool:
        return self.status in OK_RUN_STATUSES and not self.unresolved

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "attempts": [{
                "pr_number": attempt.pr_number,
                "attempt_id": attempt.attempt_id,
                "recovered": attempt.recovered,
                "turn_ok": attempt.turn_ok,
                "turn_error": attempt.turn_error,
                "timed_out": attempt.timed_out,
                "persisted_outcome": attempt.persisted_outcome,
                "owner_released": attempt.owner_released,
                "resolved": attempt.resolved,
            } for attempt in self.attempts],
            "unresolved_pr_numbers": [attempt.pr_number for attempt in self.unresolved],
        }


@dataclass(frozen=True)
class DrainConfig:
    runner_id: str
    agent_id: str = "main"
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.runner_id, str) or not OWNER_ID_RE.fullmatch(self.runner_id):
            raise ConfigError("runner ID is malformed")
        if not isinstance(self.agent_id, str) or not AGENT_ID_RE.fullmatch(self.agent_id):
            raise ConfigError("OpenClaw agent ID is malformed")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) \
                or self.max_attempts < 1:
            raise ConfigError("max_attempts must be a positive integer")


def session_ref_for(agent_id: str, runner_id: str) -> str:
    """Create a one-use OpenClaw session key accepted by queue.py's ID contract."""
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise ConfigError("OpenClaw agent ID is malformed")
    runner_tag = re.sub(r"[^A-Za-z0-9._-]", "-", runner_id)[:32] or "runner"
    return "agent:%s:jobs-runner-%s-%s" % (agent_id, runner_tag, uuid.uuid4().hex)


class DrainRunner:
    def __init__(self, config: DrainConfig, queue: QueueClient, agent: AgentAdapter,
                 session_factory: Optional[Callable[[str], str]] = None):
        self.config, self.queue, self.agent = config, queue, agent
        self._session = session_factory or (lambda runner_id: session_ref_for(
            config.agent_id, runner_id))

    def run(self) -> RunReport:
        report = RunReport("limit_reached")
        claimed_count = 0
        while claimed_count < self.config.max_attempts:
            stop = self._recover_owner(report)
            if stop:
                return stop
            session_ref = self._session(self.config.runner_id)
            try:
                claimed = self.queue.claim_next(self.config.runner_id, session_ref)
            except Exception as exc:
                report.status, report.detail = "claim_failed", type(exc).__name__
                return report
            if claimed is None:
                report.status = "uncertain" if report.unresolved else "idle"
                return report
            binding = parse_binding(claimed)
            if (binding is None or binding.runner_id != self.config.runner_id
                    or binding.session_ref != session_ref):
                report.status, report.detail = "claim_invalid", "owner_tuple"
                return report
            claimed_count += 1
            stop = self._run_claimed(report, binding)
            if stop:
                return stop
        report.status = "uncertain" if report.unresolved else "limit_reached"
        return report

    def _recover_owner(self, report: RunReport) -> Optional[RunReport]:
        try:
            owner = self.queue.browser_owner()
        except Exception as exc:
            report.status, report.detail = "owner_unreadable", type(exc).__name__
            return report
        if owner is None:
            return None
        binding = parse_binding(owner)
        if binding is None:
            report.status, report.detail = "owner_malformed", "owner_tuple"
            return report
        attempt = AttemptReport(binding.pr_number, binding.attempt_id, recovered=True)
        report.attempts.append(attempt)
        return self._finalize(binding, attempt, turn_result=None, report=report)

    def _run_claimed(self, report: RunReport, binding: Binding) -> Optional[RunReport]:
        attempt = AttemptReport(binding.pr_number, binding.attempt_id)
        report.attempts.append(attempt)
        request = TurnRequest(binding.runner_id, binding.session_ref,
                              binding.pr_number, binding.attempt_id)
        interrupted = False
        result: Optional[TurnResult] = None
        try:
            received = self.agent.start_turn(request)
            if isinstance(received, TurnResult):
                result = received
                attempt.turn_ok = received.exit_ok
                attempt.timed_out = received.timed_out
            else:
                attempt.turn_error = "invalid_adapter_result"
        except KeyboardInterrupt:
            attempt.turn_error = "interrupted"
            interrupted = True
        except Exception as exc:
            attempt.turn_error = type(exc).__name__

        stop = self._finalize(binding, attempt, result, report)
        if stop:
            return stop
        if interrupted:
            report.status = "interrupted"
            return report
        return None

    def _evidence(self, binding: Binding, turn_result: Optional[TurnResult]) -> Dict[str, Any]:
        evidence: Dict[str, Any] = {
            "schema": RUNNER_EVIDENCE_SCHEMA,
            "pr_number": binding.pr_number,
            "attempt_id": binding.attempt_id,
            "runner_id": binding.runner_id,
            "session_ref": binding.session_ref,
            "source": "turn" if turn_result is not None else "recovery",
        }
        if turn_result is not None:
            evidence["client_exit_ok"] = turn_result.exit_ok
            evidence["client_timed_out"] = turn_result.timed_out
            if turn_result.run_id:
                evidence["run_id"] = turn_result.run_id
        return evidence

    def _finalize(self, binding: Binding, attempt: AttemptReport,
                  turn_result: Optional[TurnResult], report: RunReport) -> Optional[RunReport]:
        """Ask queue.py to run its claim-pinned verifier, then read the owner back."""
        result: Any = None
        try:
            result = self.queue.finish_attempt(
                binding.pr_number, binding.attempt_id, binding.runner_id,
                binding.session_ref, self._evidence(binding, turn_result))
        except Exception as exc:
            attempt.turn_error = attempt.turn_error or "finish_" + type(exc).__name__

        try:
            owner = self.queue.browser_owner()
        except Exception as exc:
            report.status, report.detail = "slot_not_verified", type(exc).__name__
            return report
        if owner is not None:
            current = parse_binding(owner)
            report.status = "owner_preserved" if current == binding else "owner_changed"
            report.detail = "finish_or_verifier_not_confirmed"
            return report

        attempt.owner_released = True
        if isinstance(result, dict) and self._finish_matches(result, binding):
            outcome = result.get("outcome")
            if isinstance(outcome, str) and outcome in QUEUE_OUTCOMES:
                attempt.persisted_outcome = outcome
        attempt.resolved = attempt.persisted_outcome in RESOLVED_OUTCOMES
        # outcome_unknown remains visible for reconciliation, but a verified
        # free slot permits the shared queue selector to choose distinct work.
        return None

    @staticmethod
    def _finish_matches(result: Dict[str, Any], binding: Binding) -> bool:
        return (result.get("pr_number") == binding.pr_number
                and result.get("attempt_id") == binding.attempt_id
                and result.get("runner_id") == binding.runner_id
                and result.get("session_ref") == binding.session_ref
                and result.get("browser_owner_released") is True)


# Installed scheduler read-back helpers. Payloads are compared by digest and
# are never printed by this command.
REQUIRED_READBACK_FIELDS = ("schedule", "payload", "enabled")
OPTIONAL_READBACK_FIELDS = ("agentId", "sessionTarget", "delivery")


@dataclass(frozen=True)
class ReadbackResult:
    ok: bool
    mismatches: Tuple[str, ...] = ()


def expected_digests(effective_config: Dict[str, Any],
                     fields: Tuple[str, ...] = REQUIRED_READBACK_FIELDS + OPTIONAL_READBACK_FIELDS
                     ) -> Dict[str, str]:
    return {name: config_digest(effective_config.get(name)) for name in fields}


def verify_scheduler_readback(listing: Any, job_id: str,
                              expected: Dict[str, str]) -> ReadbackResult:
    problems: List[str] = []
    if not isinstance(expected, dict):
        return ReadbackResult(False, ("expected:invalid",))
    for name in REQUIRED_READBACK_FIELDS:
        if name not in expected:
            problems.append(name + ":no_expected_digest")
    if problems:
        return ReadbackResult(False, tuple(problems))
    if (not isinstance(listing, dict) or listing.get("hasMore")
            or not isinstance(listing.get("jobs"), list)):
        return ReadbackResult(False, ("listing:incomplete",))
    matches = [job for job in listing["jobs"]
               if isinstance(job, dict) and job.get("id") == job_id]
    if len(matches) != 1:
        return ReadbackResult(False, ("job:expected_one_match",))
    job = matches[0]
    for name in sorted(expected):
        want = expected[name]
        if not isinstance(want, str) or not re.fullmatch(r"[0-9a-f]{64}", want):
            problems.append(name + ":invalid_expected_digest")
        elif name in REQUIRED_READBACK_FIELDS and name not in job:
            problems.append(name + ":missing")
        elif config_digest(job.get(name)) != want:
            problems.append(name + ":digest_mismatch")
    return ReadbackResult(not problems, tuple(problems))


def _cmd_verify_readback(args: argparse.Namespace) -> int:
    try:
        listing = json.load(sys.stdin)
        with open(args.expected, encoding="utf-8") as stream:
            expected = json.load(stream)
    except (OSError, ValueError):
        print("openclaw-drain: readback inputs unreadable", file=sys.stderr)
        return 2
    result = verify_scheduler_readback(listing, args.job_id, expected)
    print(json.dumps({"ok": result.ok, "mismatches": list(result.mismatches)}))
    return 0 if result.ok else 1


def _positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(name + " must be a positive integer") from None
    if number < 1 or not math.isfinite(number):
        raise ConfigError(name + " must be a positive integer")
    return number


def _positive_float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(name + " must be a positive number") from None
    if number < 1 or not math.isfinite(number):
        raise ConfigError(name + " must be a positive number")
    return number


def check_openclaw_version(binary: str,
                           run: Callable[..., Any] = subprocess.run) -> bool:
    try:
        proc = run([binary, "--version"], capture_output=True, text=True,
                   timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == SUPPORTED_OPENCLAW_VERSION


def _run_command(args: argparse.Namespace) -> int:
    try:
        if not check_openclaw_version(args.openclaw_bin):
            raise ConfigError("unsupported or unavailable OpenClaw CLI")
        runner_id = args.runner_id or "jobs-runner-" + uuid.uuid4().hex[:20]
        config = DrainConfig(runner_id, args.agent_id, args.max_attempts)
        queue = SubprocessQueueClient(args.queue_script, args.python)
        agent = OpenClawAgentAdapter(args.openclaw_bin, args.agent_id,
                                     args.queue_script, args.turn_timeout)
        report = DrainRunner(config, queue, agent).run()
    except (ConfigError, QueueError) as exc:
        print("openclaw-drain: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("openclaw-drain: run failed: " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="claim and drain queue work through OpenClaw")
    run.add_argument("--queue-script", default=os.environ.get(
        "JOBSEARCH_QUEUE_SCRIPT", str(Path(__file__).with_name("queue.py"))))
    run.add_argument("--python", default=os.environ.get("JOBSEARCH_PYTHON", sys.executable))
    run.add_argument("--openclaw-bin", default=os.environ.get("JOBSEARCH_OPENCLAW_BIN", "openclaw"))
    run.add_argument("--agent-id", default=os.environ.get("JOBSEARCH_OPENCLAW_AGENT", "main"))
    run.add_argument("--runner-id", default=os.environ.get("JOBSEARCH_RUNNER_ID"))
    run.add_argument("--max-attempts", type=int, default=_positive_int_env(
        "JOBSEARCH_DRAIN_MAX_ATTEMPTS", 1))
    run.add_argument("--turn-timeout", type=float, default=_positive_float_env(
        "JOBSEARCH_OPENCLAW_TIMEOUT_SECONDS", 600.0))
    rb = sub.add_parser("verify-readback",
                        help="compare installed cron config to digests on stdin")
    rb.add_argument("--job-id", required=True)
    rb.add_argument("--expected", required=True, help="JSON object of field -> sha256")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        parser = build_parser()
    except ConfigError as exc:
        print("openclaw-drain: configuration_error: " + str(exc), file=sys.stderr)
        return 2
    args = parser.parse_args(argv)
    if args.command == "verify-readback":
        return _cmd_verify_readback(args)
    if args.max_attempts < 1 or not math.isfinite(args.turn_timeout) \
            or args.turn_timeout < 1:
        parser.error("max-attempts and turn-timeout must be positive")
    return _run_command(args)


if __name__ == "__main__":
    sys.exit(main())
