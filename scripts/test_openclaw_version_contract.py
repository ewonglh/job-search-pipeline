"""Synthetic checks for the shared exact-build OpenClaw version gate."""

import os
import sys
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import openclaw_drain as drain
import openclaw_ownership as ownership
from openclaw_version import SUPPORTED_OPENCLAW_BUILDS


SUPPORTED = (
    "OpenClaw 2026.10.1 (e178c39)",
    "OpenClaw 2026.11.8 (2d4ab29)",
)
REJECTED = (
    "OpenClaw 2026.11.8 (changed)",
    "OpenClaw 2026.12.0 (abcdef0)",
    "OpenClaw 2026.11.8",
    "OpenClaw 2026.10.1 (e178c39)\nOpenClaw 2026.11.8 (2d4ab29)",
    "\nOpenClaw 2026.11.8 (2d4ab29)\n",
    "OpenClaw 2026.11.8 (2d4ab29)\n\n",
    "OpenClaw 2026.11.8 (2d4ab29)\r\n\r\n",
    " OpenClaw 2026.11.8 (2d4ab29)",
    "OpenClaw 2026.11.8 (2d4ab29) ",
    "OpenClaw 2026.11.8 (2d4ab29)\r",
    None,
    b"OpenClaw 2026.11.8 (2d4ab29)",
)


def _assert_both_gates(output, returncode, expected):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=output)

    assert drain.check_openclaw_version("/opt/openclaw", run=run) is expected
    assert calls == [(["/opt/openclaw", "--version"], {
        "capture_output": True, "text": True, "timeout": 5, "check": False,
    })]

    calls.clear()
    with mock.patch.object(ownership.subprocess, "run", side_effect=run):
        if expected:
            ownership._check_version("/opt/openclaw")
        else:
            try:
                ownership._check_version("/opt/openclaw")
            except ownership.VerificationError as exc:
                assert str(exc) == "OpenClaw version is unsupported"
            else:
                raise AssertionError("ownership verifier accepted an unsupported version")
    assert calls == [(["/opt/openclaw", "--version"], {
        "capture_output": True, "text": True, "timeout": 5, "check": False,
    })]


def test_runner_and_verifier_share_exact_build_decision_and_bounded_cli_call():
    assert drain.supports_version_output is ownership.supports_version_output
    assert SUPPORTED_OPENCLAW_BUILDS == frozenset(SUPPORTED)

    for output in SUPPORTED:
        for ending in ("", "\n", "\r\n"):
            _assert_both_gates(output + ending, returncode=0, expected=True)
    for output in REJECTED:
        _assert_both_gates(output, returncode=0, expected=False)
    _assert_both_gates(SUPPORTED[1], returncode=1, expected=False)


def test_version_command_execution_failure_keeps_each_entrypoints_error_contract():
    def fail_to_run(*args, **kwargs):
        raise OSError()

    assert not drain.check_openclaw_version("/opt/openclaw", run=fail_to_run)
    with mock.patch.object(ownership.subprocess, "run", side_effect=OSError()):
        try:
            ownership._check_version("/opt/openclaw")
        except ownership.VerificationError as exc:
            assert str(exc) == "OpenClaw version check failed: OSError"
        else:
            raise AssertionError("ownership verifier accepted a failed version command")


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
    print("test_openclaw_version_contract: %d checks passed" % len(TESTS))
