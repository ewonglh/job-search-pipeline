#!/usr/bin/env python3
"""Offline checks for the queue-owned browser attempt lifecycle."""
import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from threading import Barrier
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("job_queue", os.path.join(HERE, "queue.py"))
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


VERIFIER_ID = "synthetic-browser-boundary"
EVIDENCE = '{"observation":"synthetic browser session stopped"}'


def row(pr, stamp="2026-11-01T00:00:00Z"):
    return {
        "company": "Synthetic Co %s" % pr,
        "role": "Software Engineer Intern",
        "source": "Synthetic ATS",
        "link": "https://jobs.example.invalid/%s" % pr,
        "variant": "variants/synthetic-%s/resume.tex" % pr,
        "pr": "https://github.com/example/resume/pull/%s" % pr,
        "pr_number": pr,
        "enqueued_at": stamp,
        "status": "queued",
    }


def make_case(directory, rows=None):
    path = os.path.join(directory, "queue.yaml")
    q.write_queue(path, rows or [row(12), row(14)])
    return path


def claim_patches(barrier=None):
    def inputs(args, item, items, log):
        if barrier is not None:
            barrier.wait(timeout=5)
        return {
            "posting": {"company": item["company"], "role": item["role"],
                        "source": item["source"]},
            "apply_url": "https://apply.example.invalid/%s" % item["pr_number"],
            "decision": {"status": "eligible", "reason_codes": [], "provenance": {}},
            "profile_path": "/synthetic/profile.yaml",
            "profile_sha256": "p" * 64,
            "postings_sha256": "j" * 64,
            "tracker_revision": q.TrackerRevision(
                "a" * 40, "[]\n", "t" * 64),
            "tracker_sha256": "t" * 64,
            "skip_sha256": "s" * 64,
            "gate_sha256": "g" * 64,
            "jd_sha256": "d" * 64,
            "application_id": q._canonical_queue_identity(item),
        }

    def recheck(args, item, items, posting, revision):
        return ("p" * 64, "j" * 64, "t" * 64, "s" * 64, "g" * 64)

    return mock.patch.object(q, "_read_claim_inputs", side_effect=inputs), \
        mock.patch.object(q, "_read_claim_validation_inputs", side_effect=recheck)


def cli(args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        rc = q.main(args)
    return rc, output.getvalue()


def claim_next(path, runner="runner-a", session="session-a"):
    return claim_next_with_verifier(path, shlex.quote(sys.executable), runner, session)


def claim_next_with_verifier(path, verifier_command, runner="runner-a", session="session-a"):
    patches = claim_patches()
    with patches[0], patches[1], mock.patch.dict(
            os.environ, {"JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": verifier_command}):
        rc, output = cli(["claim", "--queue", path, "--next", "--runner-id", runner,
                          "--session-ref", session, "--json"])
    return rc, json.loads(output) if output.strip() else None


def verifier(request):
    owner = request["owner"]
    return {
        "schema": "job-search-browser-termination-verdict/v1",
        "verified": True,
        "challenge": request["challenge"],
        "pr_number": owner["pr_number"],
        "attempt_id": owner["attempt_id"],
        "runner_id": owner["runner_id"],
        "session_ref": owner["session_ref"],
        "state": "fenced",
        "in_flight_rejected": True,
        "verifier_id": VERIFIER_ID,
    }


def finish(path, claimed, evidence=EVIDENCE):
    return cli(["finish-attempt", "--queue", path,
                "--pr", str(claimed["pr_number"]),
                "--attempt-id", claimed["attempt_id"],
                "--runner-id", claimed["runner_id"],
                "--session-ref", claimed["session_ref"],
                "--termination-evidence", evidence, "--json"])


def make_external_verifier(directory, executable=False):
    path = os.path.join(directory, "synthetic_verifier.py")
    source = textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, os, sys
        request = json.load(sys.stdin)
        log_path = os.environ.get("VERIFIER_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(request["challenge"] + "\\n")
        owner = request["owner"]
        print(json.dumps({
            "schema": "job-search-browser-termination-verdict/v1",
            "verified": True,
            "challenge": request["challenge"],
            "pr_number": owner["pr_number"],
            "attempt_id": owner["attempt_id"],
            "runner_id": owner["runner_id"],
            "session_ref": owner["session_ref"],
            "state": "fenced",
            "in_flight_rejected": True,
            "verifier_id": "synthetic-process-verifier",
        }))
        """)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(source)
    if executable:
        os.chmod(path, 0o700)
        return path, shlex.quote(path)
    return path, shlex.quote(sys.executable) + " " + shlex.quote(path)


# Simultaneous --next requests can reserve only one browser. The expired lease
# remains an owner until the independent verifier fences it.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12), row(14)])
    barrier = Barrier(2)
    patches = claim_patches(barrier)

    results = []

    def concurrent_claim(runner):
        results.append(q.main(["claim", "--queue", path, "--next", "--runner-id", runner,
                               "--session-ref", "session-" + runner, "--json"]))

    with patches[0], patches[1], \
            mock.patch.dict(os.environ, {
                "JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": shlex.quote(sys.executable)}), \
            mock.patch("builtins.print", lambda *args, **kwargs: None):
        threads = [threading.Thread(target=concurrent_claim, args=(runner,))
                   for runner in ("runner-a", "runner-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [0, 2], results
    owner_rc, owner_text = cli(["browser-owner", "--queue", path, "--json"])
    assert owner_rc == 0
    owner = json.loads(owner_text)
    assert owner["pr_number"] == 12 and owner["attempt_id"]
    assert q.load_queue(path)[0]["legacy_browser_owner"] == "false"
    assert json.loads(q.load_queue(path)[0]["browser_owner_verifier_command"])[0] == os.path.realpath(sys.executable)
    assert len(q.load_queue(path)[0]["browser_owner_verifier_sha256"]) == 64
    assert len(q.load_queue(path)[0]["browser_owner_verifier_command_sha256"]) == 64

    rows = q.load_queue(path)
    rows[0]["lease_expires_at"] = "2000-01-01T00:00:00Z"
    q.write_queue(path, rows)
    owner_text = cli(["browser-owner", "--queue", path, "--json"])[1]
    assert json.loads(owner_text)["lease_expired"] is True
    blocked_rc, _ = cli(["claim", "--queue", path, "--next", "--runner-id",
                        "runner-c", "--session-ref", "session-c", "--json"])
    assert blocked_rc == 2

    # A caller environment change cannot replace the pinned command. This owner
    # pins Python itself, which is not a verifier, so its failed response keeps
    # the browser reservation intact.
    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", None), \
            mock.patch.dict(os.environ, {"JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": ""}):
        finish_rc, _ = finish(path, owner)
    assert finish_rc == 2
    assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is not None

    # Wrong attempt tuple cannot write the outcome. A concrete pre-submit block
    # is durable, but still keeps browser ownership until verification succeeds.
    mismatch_rc, _ = cli(["status", "--queue", path, "--pr", "12", "--status", "blocked",
                         "--attempt-id", owner["attempt_id"], "--runner-id", "wrong-runner",
                         "--session-ref", owner["session_ref"], "--notes", "CAPTCHA blocks progress",
                         "--evidence-source", "employer application form",
                         "--evidence-observation", "CAPTCHA blocks the form before it can continue."])
    assert mismatch_rc == 2
    blocked_rc, _ = cli(["status", "--queue", path, "--pr", "12", "--status", "blocked",
                         "--attempt-id", owner["attempt_id"], "--runner-id", owner["runner_id"],
                         "--session-ref", owner["session_ref"], "--notes", "CAPTCHA blocks progress",
                         "--evidence-source", "employer application form",
                         "--evidence-observation", "CAPTCHA blocks the form before it can continue."])
    assert blocked_rc == 0
    assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is not None

    # A verifier response must reject in-flight work. The same finish tuple is
    # idempotent after successful release and never invokes it a second time.
    def incomplete_fence(request):
        result = verifier(request)
        result.pop("in_flight_rejected")
        return result

    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", incomplete_fence):
        assert finish(path, owner)[0] == 2
    assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is not None
    calls = []

    def valid_fence(request):
        calls.append(request["challenge"])
        return verifier(request)

    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", valid_fence):
        finish_rc, finish_text = finish(path, owner)
        assert finish_rc == 0, finish_text
        assert finish_text.strip(), "finish-attempt returned no JSON"
        result = json.loads(finish_text)
        assert result["outcome"] == "blocked" and result["browser_owner_released"]
        again_rc, again_text = finish(path, owner)
        assert again_rc == 0 and json.loads(again_text)["outcome"] == "blocked"
    assert len(calls) == 1
    assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is None

    # Resume requires evidence that clears the blocker, and archives the old attempt.
    resume_rc, _ = cli(["resume", "--queue", path, "--pr", "12",
                        "--attempt-id", owner["attempt_id"],
                        "--evidence-source", "employer application form",
                        "--evidence-observation",
                        "The CAPTCHA was cleared and the form is available to continue.",
                        "--json"])
    assert resume_rc == 0
    resumed = q.load_queue(path)[0]
    assert resumed["status"] == "queued" and not resumed.get("attempt_id")
    assert json.loads(resumed["attempt_history"])[0]["attempt_id"] == owner["attempt_id"]


# An explicit PR cannot bypass the common oldest-first selector, and every new
# claim requires a bound runner/session tuple.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12), row(14)])
    patches = claim_patches()
    with patches[0], patches[1], mock.patch.dict(
            os.environ, {"JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": shlex.quote(sys.executable)}):
        assert cli(["claim", "--queue", path, "--pr", "14", "--runner-id", "runner-a",
                    "--session-ref", "session-a", "--json"])[0] == 2
        assert cli(["claim", "--queue", path, "--pr", "12", "--json"])[0] == 2
    assert all(item["status"] == "queued" for item in q.load_queue(path))

with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12)])
    patches = claim_patches()
    with patches[0], patches[1], mock.patch.dict(
            os.environ, {"JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": ""}):
        assert cli(["claim", "--queue", path, "--next", "--runner-id", "runner-a",
                    "--session-ref", "session-a", "--json"])[0] == 2
    assert q.load_queue(path)[0]["status"] == "queued"


# A real verifier subprocess comes from the claim-time pin even if a finish
# caller replaces the environment setting.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12)])
    log_path = os.path.join(directory, "verifier.log")
    verifier_path, command = make_external_verifier(directory)
    rc, claimed = claim_next_with_verifier(path, command)
    assert rc == 0
    with mock.patch.dict(os.environ, {
            "JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": "a-command-that-must-not-run",
            "VERIFIER_LOG": log_path}):
        finish_rc, finish_text = finish(path, claimed)
    assert finish_rc == 0, finish_text
    assert os.path.exists(log_path)
    assert json.loads(finish_text)["browser_owner_released"] is True


# Replacing the persisted command or changing the executable bytes fails closed.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12)])
    _, command = make_external_verifier(directory)
    rc, claimed = claim_next_with_verifier(path, command)
    assert rc == 0
    rows = q.load_queue(path)
    rows[0]["browser_owner_verifier_command"] = json.dumps([os.path.realpath(sys.executable)])
    q.write_queue(path, rows)
    assert finish(path, claimed)[0] == 2
    assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is not None

if os.name == "posix":
    with tempfile.TemporaryDirectory() as directory:
        path = make_case(directory, [row(12)])
        verifier_path, command = make_external_verifier(directory, executable=True)
        rc, claimed = claim_next_with_verifier(path, command)
        assert rc == 0
        with open(verifier_path, "a", encoding="utf-8") as fh:
            fh.write("# changed after claim\\n")
        assert finish(path, claimed)[0] == 2
        assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is not None


# The process race reaches the real cross-process queue lock: both workers pass
# the initial read and eligibility phase before contending on the final write.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12), row(14)])
    barrier = os.path.join(directory, "barrier")
    os.mkdir(barrier)
    worker = os.path.join(directory, "race_worker.py")
    with open(worker, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""\
            import importlib.util, os, sys, time
            from unittest import mock
            scripts, queue_path, barrier, runner = sys.argv[1:]
            sys.path.insert(0, scripts)
            spec = importlib.util.spec_from_file_location("job_queue", os.path.join(scripts, "queue.py"))
            q = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(q)
            def inputs(args, item, items, log):
                open(os.path.join(barrier, runner + ".ready"), "w").close()
                deadline = time.time() + 15
                while len([name for name in os.listdir(barrier) if name.endswith(".ready")]) < 2:
                    if time.time() > deadline:
                        raise RuntimeError("subprocess claim barrier timed out")
                    time.sleep(.01)
                return {
                    "posting": {key: item[key] for key in ("company", "role", "source")},
                    "apply_url": "https://apply.example.invalid/%s" % item["pr_number"],
                    "decision": {"status": "eligible", "reason_codes": [], "provenance": {}},
                    "profile_path": "/synthetic/profile.yaml", "profile_sha256": "p" * 64,
                    "postings_sha256": "j" * 64,
                    "tracker_revision": q.TrackerRevision("a" * 40, "[]\\n", "t" * 64),
                    "tracker_sha256": "t" * 64, "skip_sha256": "s" * 64,
                    "gate_sha256": "g" * 64, "jd_sha256": "d" * 64,
                    "application_id": q._canonical_queue_identity(item),
                }
            def recheck(args, item, items, posting, revision):
                return ("p" * 64, "j" * 64, "t" * 64, "s" * 64, "g" * 64)
            with mock.patch.object(q, "_read_claim_inputs", side_effect=inputs), \\
                    mock.patch.object(q, "_read_claim_validation_inputs", side_effect=recheck):
                sys.exit(q.main(["claim", "--queue", queue_path, "--next", "--runner-id", runner,
                                 "--session-ref", "session-" + runner, "--json"]))
            """))
    env = dict(os.environ, JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER=shlex.quote(sys.executable))
    processes = [subprocess.Popen(
        [sys.executable, worker, os.path.dirname(q.__file__), path, barrier, runner],
        cwd=os.path.dirname(q.__file__), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for runner in ("process-a", "process-b")]
    outputs = [process.communicate(timeout=25) for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 2], outputs
    assert q.load_queue(path)[0]["status"] == "in_progress"


# An interrupted attempt with no recorded outcome becomes unknown even when the
# pre-submit marker is absent. After verified release, different work can claim.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12), row(14)])
    rc, claimed = claim_next(path)
    assert rc == 0 and claimed["pr_number"] == 12
    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", verifier):
        finish_rc, finish_text = finish(path, claimed)
    assert finish_rc == 0
    assert json.loads(finish_text)["outcome"] == "outcome_unknown"
    row12 = q.load_queue(path)[0]
    assert row12["status"] == "outcome_unknown" and row12["attempt_id"] == claimed["attempt_id"]
    rc, next_claim = claim_next(path, "runner-b", "session-b")
    assert rc == 0 and next_claim["pr_number"] == 14


# A legacy claim remains a synthetic, unbound owner through stale recovery and
# application reconciliation; neither operation proves its browser is stopped.
with tempfile.TemporaryDirectory() as directory:
    legacy = row(12)
    legacy.update({"status": "in_progress", "attempt_id": "legacy-attempt-12",
                   "attempt_state": "authorized", "submit_gate": "durable",
                   "claimed_at": "2000-01-01T00:00:00Z"})
    path = make_case(directory, [legacy, row(14)])
    assert cli(["release-stale", "--queue", path])[0] == 0
    owner = json.loads(cli(["browser-owner", "--queue", path, "--json"])[1])
    assert owner["legacy"] is True and owner["attempt_id"] == "legacy-attempt-12"
    retry_rc, _ = cli(["reconcile", "--queue", path, "--pr", "12",
                       "--attempt-id", "legacy-attempt-12", "--result", "retry",
                       "--evidence-source", "employer portal application history",
                       "--evidence-observation",
                       "history shows no submission for this posting; it remains a draft"])
    assert retry_rc == 0
    owner = json.loads(cli(["browser-owner", "--queue", path, "--json"])[1])
    assert owner["legacy"] is True and owner["attempt_id"] == "legacy-attempt-12"
    assert cli(["claim", "--queue", path, "--next", "--runner-id", "runner-b",
                "--session-ref", "session-b", "--json"])[0] == 2
    missing_verifier_error = io.StringIO()
    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", None), \
            mock.patch.dict(os.environ, {"JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": ""}), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(missing_verifier_error):
        missing_verifier_rc = q.main([
            "finish-attempt", "--queue", path, "--pr", "12",
            "--attempt-id", "legacy-attempt-12", "--runner-id", "legacy-unbound",
            "--session-ref", "legacy-unbound", "--termination-evidence", EVIDENCE, "--json"])
    assert missing_verifier_rc == 2
    assert "legacy browser owner has no pinned verifier" in missing_verifier_error.getvalue()
    assert json.loads(cli(["browser-owner", "--queue", path, "--json"])[1]) is not None
    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", verifier), \
            mock.patch.dict(os.environ, {
                "JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": shlex.quote(sys.executable)}):
        finish_rc, finish_text = finish(path, owner)
    assert finish_rc == 0 and json.loads(finish_text)["outcome"] == "retry"
    rc, next_claim = claim_next(path, "runner-b", "session-b")
    assert rc == 0 and next_claim["pr_number"] == 12


# Confirmed submission survives finish unchanged, while a begin-submit marker
# makes an otherwise unreported result outcome_unknown.
with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12)])
    rc, claimed = claim_next(path)
    assert rc == 0
    rows = q.load_queue(path)
    item = rows[0]
    item.update({
        "status": "submitted", "attempt_state": "submitted",
        "submitted_attempt_id": claimed["attempt_id"], "submitted_at": "2026-11-23",
        "sent": "Synthetic PDF", "confirmation_evidence": "Employer portal confirmed receipt.",
    })
    q.write_queue(path, rows)
    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", verifier):
        finish_rc, finish_text = finish(path, claimed)
    assert finish_rc == 0 and json.loads(finish_text)["outcome"] == "submitted"
    submitted = q.load_queue(path)[0]
    assert submitted["status"] == "submitted"
    assert submitted["confirmation_evidence"] == "Employer portal confirmed receipt."
    assert not submitted.get("browser_owner_attempt_id")


with tempfile.TemporaryDirectory() as directory:
    path = make_case(directory, [row(12)])
    rc, claimed = claim_next(path)
    assert rc == 0
    rows = q.load_queue(path)
    item = rows[0]
    app_id = q._canonical_queue_identity(item)
    item.update({
        "prepared_snapshot_id": "synthetic-snapshot",
        "prepared_snapshot_attempt_id": claimed["attempt_id"],
        "prepared_snapshot_application_id": app_id,
        "snapshot_verified_id": "synthetic-snapshot",
        "snapshot_verified_attempt_id": claimed["attempt_id"],
        "snapshot_verified_application_id": app_id,
        "snapshot_verified_at": q.now_utc(),
    })
    q.write_queue(path, rows)
    marker_args = ["begin-submit", "--queue", path, "--pr", "12",
                   "--attempt-id", claimed["attempt_id"], "--runner-id", claimed["runner_id"],
                   "--session-ref", claimed["session_ref"], "--json"]
    assert cli(marker_args)[0] == 0
    marker_at = q.load_queue(path)[0]["submit_started_at"]
    assert cli(marker_args)[0] == 0
    assert q.load_queue(path)[0]["submit_started_at"] == marker_at
    blocked_rc, _ = cli(["status", "--queue", path, "--pr", "12", "--status", "blocked",
                         "--attempt-id", claimed["attempt_id"], "--runner-id", claimed["runner_id"],
                         "--session-ref", claimed["session_ref"], "--notes", "CAPTCHA blocks progress",
                         "--evidence-source", "employer application form",
                         "--evidence-observation", "CAPTCHA blocks the form before it can continue."])
    assert blocked_rc == 2
    with mock.patch.object(q, "TERMINATION_VERIFIER_CALLBACK", verifier):
        finish_rc, finish_text = finish(path, claimed)
    assert finish_rc == 0 and json.loads(finish_text)["outcome"] == "outcome_unknown"
    assert q.load_queue(path)[0]["status"] == "outcome_unknown"

print("test_attempt_lifecycle: all checks passed")
