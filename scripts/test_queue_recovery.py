#!/usr/bin/env python3
"""Recovery, synchronization, and cross-process queue contract checks."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from unittest import mock

import queue as q


PROFILE = """availability: 1 May 2027 to 31 August 2027
degree: Bachelor of Computing
degree_completed: No
expected_graduation: 2028-05
current_year: Year 2
program_length_years: 4
citizenship: Singapore citizen
work_authorisation_country: Singapore
work_authorisation: Valid student authorization in Singapore without employer sponsorship
credit_bearing_internship: Yes
school_approval: Yes
"""
JD = "Internship period: 9 May 2027 to 1 August 2027."
LINK = "https://fixture.invalid/jobs/12"


def make_case(root, status="queued"):
    root = os.fspath(root)
    repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(repo, "data"), exist_ok=True)
    profile = os.path.join(repo, "data", "profile.yaml")
    with open(profile, "w", encoding="utf-8") as fh:
        fh.write(PROFILE)
    with open(os.path.join(repo, "data", "postings.json"), "w", encoding="utf-8") as fh:
        json.dump([{"company": "Fixture Co", "role": "Software Engineering Intern",
                    "source": "Company ATS", "link": LINK}], fh)
    with open(os.path.join(repo, "applications.yaml"), "w", encoding="utf-8") as fh:
        fh.write("[]\n")
    skipped = os.path.join(root, "skipped.yaml")
    gates = os.path.join(root, "gates.yaml")
    for path in (skipped, gates):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
    queue = os.path.join(root, "queue.yaml")
    item = {"company": "Fixture Co", "role": "Software Engineering Intern",
            "source": "Company ATS", "link": LINK,
            "pr": "https://github.com/example/resume/pull/12", "pr_number": 12,
            "status": status, "variant": "variants/fixture-co-swe/resume.tex",
            "notes": "preserve notes", "owner_field": "preserve owner data"}
    if status == "in_progress":
        item.update({"attempt_id": "attempt-12", "attempt_state": "authorized",
                     "submit_gate": "durable", "claimed_at": q.now_utc()})
        item["claim_application_id"] = q._canonical_queue_identity(item)
    q.write_queue(queue, [item])
    case = {"root": root, "repo": repo, "profile": profile,
            "skipped": skipped, "gates": gates, "queue": queue}
    if status == "in_progress":
        q.SNAPSHOT_ROOT = os.path.join(root, "private-snapshots")
        pdf = os.path.join(root, "selected.pdf")
        answers = os.path.join(root, "selected.json")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-1.7\nSynthetic fixture resume\n")
        with open(answers, "w", encoding="utf-8") as fh:
            fh.write('{"availability":"May to August"}\n')
        assert cli(["prepare-snapshot", "--queue", queue, "--repo", repo,
                    "--pr", "12", "--attempt-id", "attempt-12", "--pdf", pdf,
                    "--answers", answers, "--operator-verification",
                    "I checked the selected PDF and answers for this posting."]) == 0
        assert cli(["verify-snapshot", "--queue", queue, "--repo", repo,
                    "--pr", "12", "--attempt-id", "attempt-12", "--pdf", pdf,
                    "--answers", answers]) == 0
        case.update({"pdf": pdf, "answers": answers})
    return case


def cli(args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return q.main(args)


def status_args(case, status="submitted"):
    return ["status", "--queue", case["queue"], "--repo", case["repo"],
            "--pr", "12", "--status", status]


# Two independent CLI processes pass the same current-JD check; the queue lock
# allows exactly one durable attempt marker to win.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory)
    origin = os.path.join(directory, "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    git = ["git", "-C", case["repo"]]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "applications.yaml"], check=True)
    subprocess.run(git + ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                          "commit", "-qm", "Tracker fixture"], check=True)
    subprocess.run(git + ["remote", "add", "origin", origin], check=True)
    subprocess.run(git + ["push", "-q", "origin", "HEAD:main"], check=True)
    worker = os.path.join(directory, "claim_worker.py")
    with open(worker, "w", encoding="utf-8") as fh:
        fh.write("""import os, sys, time
sys.path.insert(0, os.environ['SCRIPTS_DIR'])
import tailor
import queue as q
tailor.fetch_jd = lambda posting: {'jd_text': %r}
ready, barrier, queue_path, repo, profile, skipped, gates = sys.argv[1:]
open(ready, 'w').close()
deadline = time.time() + 15
while len([name for name in os.listdir(barrier) if name.endswith('.ready')]) < 2:
    if time.time() > deadline:
        sys.exit(90)
    time.sleep(.01)
sys.exit(q.main(['claim', '--queue', queue_path, '--repo', repo,
                 '--profile', profile, '--pr', '12']))
""" % JD)
    barrier = os.path.join(directory, "barrier")
    os.mkdir(barrier)
    env = dict(os.environ, SCRIPTS_DIR=os.path.dirname(q.__file__),
               JOBSEARCH_SKIPPED=os.path.join(directory, "skipped.yaml"),
               JOBSEARCH_GATES=os.path.join(directory, "gates.yaml"))
    workers = []
    for number in range(2):
        workers.append(subprocess.Popen(
            [sys.executable, worker, os.path.join(barrier, "%d.ready" % number),
             barrier, case["queue"], case["repo"], case["profile"],
             case["skipped"], case["gates"]], cwd=os.path.dirname(q.__file__),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    results = []
    for proc in workers:
        proc.communicate(timeout=20)
        results.append(proc.returncode)
    assert sorted(results) == [0, 2], results
    winner = q.load_queue(case["queue"])[0]
    assert winner["status"] == "in_progress" and winner["attempt_id"]


# A tracker failure happens after submitted + pending sync is durably stored.
# The injected tracker call also writes unrelated queue metadata to prove that
# no queue lock is held across network I/O and that the sync merge preserves it.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="in_progress")
    assert cli(status_args(case) + ["--attempt-id", "attempt-12",
                                   "--date", "2026-11-16", "--sent", "PDF and answers",
                                   "--confirmation-evidence", "I think it worked.",
                                   "--no-tracker"]) == 2
    assert q.load_queue(case["queue"])[0]["status"] == "in_progress"

    def failing_tracker(_repo, _item, *_args, **_kwargs):
        with q.queue_lock(case["queue"]):
            latest = q.load_queue(case["queue"])
            latest[0]["concurrent_metadata"] = "written during tracker call"
            q._write_queue_unlocked(case["queue"], latest)
        return ""

    import submission_snapshot as snapshots
    with mock.patch.object(q, "write_tracker_row", side_effect=failing_tracker), \
            mock.patch.object(snapshots, "finalize_snapshot",
                              side_effect=OSError("injected snapshot finalization fault")):
        rc = cli(status_args(case) + ["--attempt-id", "attempt-12",
                                     "--date", "2026-11-16", "--sent", "PDF and answers",
                                     "--confirmation-evidence", "Employer portal confirmed receipt."])
    assert rc == 1
    submitted = q.load_queue(case["queue"])[0]
    assert submitted["status"] == "submitted"
    assert submitted["attempt_state"] == "submitted"
    assert submitted["tracker_sync"] == "error"
    assert submitted["tracker_sync_error"] == "tracker_write_failed"
    assert submitted["snapshot_provenance"] == "incomplete"
    prepared_id = submitted["prepared_snapshot_id"]
    assert prepared_id and submitted["snapshot_error"]
    assert submitted["concurrent_metadata"] == "written during tracker call"
    assert submitted["owner_field"] == "preserve owner data"
    assert submitted["notes"] == "preserve notes"
    assert cli(["finalize-snapshot", "--queue", case["queue"], "--pr", "12",
                "--attempt-id", "attempt-12"]) == 0
    finalized = q.load_queue(case["queue"])[0]
    assert finalized["status"] == "submitted"
    assert finalized["prepared_snapshot_id"] == prepared_id
    assert finalized["snapshot_provenance"] == "complete"
    assert finalized["snapshot_id"] != prepared_id
    with mock.patch.object(q, "write_tracker_row", return_value="present"):
        assert cli(["sync-tracker", "--queue", case["queue"], "--repo", case["repo"],
                    "--pr", "12"]) == 0
    repaired = q.load_queue(case["queue"])[0]
    assert repaired["status"] == "submitted" and repaired["tracker_sync"] == "synced"
    assert repaired["submitted_at"] == "2026-11-16"
    assert repaired["sent"] == "PDF and answers"
    assert repaired["concurrent_metadata"] == "written during tracker call"


# A local confirmation write fault leaves the earlier durable marker intact.
# A fresh module load (process-restart seam) cannot claim it; stale recovery
# requires explicit non-submission evidence before retry.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="in_progress")
    real_write = q._write_queue_unlocked

    def fail_submitted_write(path, rows):
        if any(row.get("status") == "submitted" for row in rows):
            raise OSError("injected post-confirmation queue write fault")
        return real_write(path, rows)

    with mock.patch.object(q, "_write_queue_unlocked", side_effect=fail_submitted_write):
        assert cli(status_args(case) + ["--attempt-id", "attempt-12",
                                       "--date", "2026-11-16", "--sent", "PDF and answers",
                                       "--confirmation-evidence", "Employer portal confirmed receipt.",
                                       "--no-tracker"]) == 2
    durable = q.load_queue(case["queue"])[0]
    assert durable["status"] == "in_progress"
    assert durable["attempt_id"] == "attempt-12"
    assert durable["submit_gate"] == "durable"
    assert durable["prepared_snapshot_id"]

    spec = importlib.util.spec_from_file_location("restarted_queue", q.__file__)
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    assert restarted.main(["status", "--queue", case["queue"], "--pr", "12",
                           "--status", "queued", "--force"]) == 2
    durable = restarted.load_queue(case["queue"])
    durable[0]["claimed_at"] = "2000-01-01T00:00:00Z"
    restarted.write_queue(case["queue"], durable)
    assert restarted.main(["release-stale", "--queue", case["queue"]]) == 0
    unknown = restarted.load_queue(case["queue"])[0]
    assert unknown["status"] == "outcome_unknown"
    assert unknown["attempt_id"] == "attempt-12"
    assert restarted.main(["claim", "--queue", case["queue"], "--pr", "12"]) == 2
    assert restarted.main(["status", "--queue", case["queue"], "--pr", "12",
                           "--status", "queued", "--force"]) == 2
    assert restarted.main(["reconcile", "--queue", case["queue"], "--pr", "12",
                           "--attempt-id", "attempt-12", "--result", "retry",
                           "--evidence-source", "manual review",
                           "--evidence-observation", "I checked the status."]) == 2
    assert restarted.main(["reconcile", "--queue", case["queue"], "--pr", "12",
                           "--attempt-id", "attempt-12", "--result", "retry",
                           "--evidence-source", "employer portal application history",
                           "--evidence-observation",
                           "The history shows no submission for this posting; it remains a draft."]) == 0
    retried = restarted.load_queue(case["queue"])[0]
    assert retried["status"] == "queued" and not retried.get("attempt_id")
    assert json.loads(retried["attempt_history"])[0]["attempt_id"] == "attempt-12"


# Submitted and unresolved attempts remain terminal to force and free-form notes.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="submitted")
    row = q.load_queue(case["queue"])[0]
    row.update({"attempt_id": "attempt-12", "attempt_state": "submitted",
                "submitted_at": "2026-11-16", "sent": "PDF"})
    q.write_queue(case["queue"], [row])
    assert cli(["status", "--queue", case["queue"], "--pr", "12",
                "--status", "queued", "--force"]) == 2
    with mock.patch.object(q, "write_tracker_row", return_value="present"):
        assert cli(["sync-tracker", "--queue", case["queue"], "--repo", case["repo"],
                    "--pr", "12"]) == 0
    historical = q.load_queue(case["queue"])[0]
    assert historical["status"] == "submitted"
    assert not historical.get("prepared_snapshot_id")
    assert not historical.get("snapshot_id")
    assert q.load_queue(case["queue"])[0]["status"] == "submitted"


# Malformed state never falls back to an empty queue that could be overwritten.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory)
    with open(case["queue"], "w", encoding="utf-8") as fh:
        fh.write("- status: queued\nthis is not a queue row\n")
    assert cli(["release-stale", "--queue", case["queue"]]) == 1
    with open(case["queue"], encoding="utf-8") as fh:
        assert fh.read() == "- status: queued\nthis is not a queue row\n"

with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory)
    with open(case["queue"], "w", encoding="utf-8"):
        pass
    assert cli(["release-stale", "--queue", case["queue"]]) == 1
    assert os.path.getsize(case["queue"]) == 0

# A delayed confirmed success must not be lost to the two-minute pre-submit
# TTL. Preserve the old timestamp rather than fabricating a late verification.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="in_progress")
    rows = q.load_queue(case["queue"])
    rows[0]["snapshot_verified_at"] = "2000-01-01T00:00:00Z"
    q.write_queue(case["queue"], rows)
    assert cli(status_args(case) + ["--attempt-id", "attempt-12",
               "--date", "2026-11-16", "--sent", "PDF and answers",
               "--confirmation-evidence", "Employer portal confirmed receipt.",
               "--no-tracker"]) == 0
    row = q.load_queue(case["queue"])[0]
    assert row["status"] == "submitted" and row["snapshot_provenance"] == "complete"
    assert row["snapshot_verification_at_confirmation"] == "expired"
    assert row["snapshot_verified_at"] == "2000-01-01T00:00:00Z"


def reconcile_args(case):
    return ["reconcile", "--queue", case["queue"], "--repo", case["repo"],
            "--pr", "12", "--attempt-id", "attempt-12", "--result", "submitted",
            "--evidence-source", "employer portal application history",
            "--evidence-observation", "The portal shows this application was submitted.",
            "--date", "2026-11-16", "--sent", "PDF and answers",
            "--confirmation-evidence", "Employer portal confirmed receipt.", "--no-tracker"]


def recovery_args(case):
    return ["recover-submissions", "--queue", case["queue"], "--repo", case["repo"],
            "--json"]


def capture_recovery(module, case):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        rc = module.main(recovery_args(case))
    return rc, json.loads(output.getvalue()), output.getvalue()


def restarted_module():
    spec = importlib.util.spec_from_file_location("restarted_queue", q.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SNAPSHOT_ROOT = q.SNAPSHOT_ROOT
    return module


# Both confirmation routes preserve success with absent evidence. Recovery also
# finalizes retained evidence when tracker writes were explicitly disabled.
for mode in ("status_missing", "status_unverified", "reconcile", "reconcile_fault"):
    with tempfile.TemporaryDirectory() as directory:
        case = make_case(directory, status="in_progress")
        rows = q.load_queue(case["queue"])
        if mode == "status_missing":
            rows[0] = {k: v for k, v in rows[0].items()
                       if not k.startswith(("snapshot_", "prepared_snapshot_"))}
        elif mode == "status_unverified":
            rows[0].pop("snapshot_verified_at")
        else:
            rows[0]["status"] = "outcome_unknown"
            rows[0]["attempt_state"] = "outcome_unknown"
        q.write_queue(case["queue"], rows)
        args = (reconcile_args(case) if mode.startswith("reconcile") else
                status_args(case) + ["--attempt-id", "attempt-12", "--date", "2026-11-16",
                "--sent", "PDF and answers", "--confirmation-evidence",
                "Employer portal confirmed receipt.", "--no-tracker"])
        with mock.patch.object(q, "sync_tracker_attempt",
                               side_effect=AssertionError("no tracker requested")):
            if mode == "reconcile_fault":
                with mock.patch.object(q, "_snapshot_store", side_effect=OSError("storage fault")):
                    rc = cli(args)
            else:
                rc = cli(args)
        row = q.load_queue(case["queue"])[0]
        expected = {"status_missing": "unknown", "status_unverified": "incomplete",
                    "reconcile": "complete", "reconcile_fault": "incomplete"}[mode]
        assert row["status"] == "submitted" and row["attempt_state"] == "submitted"
        assert row["snapshot_provenance"] == expected, (mode, row)
        assert rc == (0 if mode == "reconcile" else 1), mode
        assert bool(row.get("snapshot_id")) == (mode in ("reconcile", "status_unverified"))
        assert bool(row.get("snapshot_error")) == (mode != "reconcile")


# All recovery evidence fields reject credentials before any state mutation;
# neither errors nor queue contents reveal the synthetic secret.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="in_progress")
    rows = q.load_queue(case["queue"])
    rows[0]["status"] = "outcome_unknown"
    q.write_queue(case["queue"], rows)
    with open(case["queue"], "rb") as stream:
        before = stream.read()
    for field in ("--confirmation-evidence", "--evidence-source", "--evidence-observation"):
        args = reconcile_args(case)
        args[args.index(field) + 1] += "; OTP: R7_SYNTHETIC_SENTINEL"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            assert q.main(args) == 2
        with open(case["queue"], "rb") as stream:
            assert stream.read() == before
        assert "R7_SYNTHETIC_SENTINEL" not in output.getvalue()

# A stop after durable confirmation leaves a terminal row and pending outbox.
# Restart finalizes the retained bytes before sync. A second stop after remote
# success replays through the idempotent writer without creating another row.
for confirmation_route in ("status", "reconcile"):
    with tempfile.TemporaryDirectory() as directory:
        case = make_case(directory, status="in_progress")
        if confirmation_route == "reconcile":
            rows = q.load_queue(case["queue"])
            rows[0].update({"status": "outcome_unknown",
                            "attempt_state": "outcome_unknown"})
            q.write_queue(case["queue"], rows)
        args = (status_args(case) + ["--attempt-id", "attempt-12",
                "--date", "2026-11-16", "--sent", "PDF and answers",
                "--confirmation-evidence", "Employer portal confirmed receipt.",
                "--no-tracker"] if confirmation_route == "status" else
                reconcile_args(case))
        with mock.patch.object(q, "_post_submission_snapshot",
                               side_effect=SystemExit("simulated stop")):
            try:
                cli(args)
            except SystemExit:
                pass
            else:
                raise AssertionError("confirmation did not reach the simulated stop")
        durable = q.load_queue(case["queue"])[0]
        assert durable["status"] == "submitted"
        assert durable["tracker_sync"] == "pending"
        assert durable["tracker_sync_pending_at"] == durable["submission_confirmed_at"]
        assert durable["tracker_sync_attempts"] == 0
        assert durable["prepared_snapshot_id"] and not durable.get("snapshot_id")
        durable.update({"browser_owner_attempt_id": "attempt-12",
                        "runner_id": "runner-12", "session_ref": "session-12",
                        "lease_expires_at": q._lease_expiry(300),
                        "browser_owner_state": "active"})
        q.write_queue(case["queue"], [durable])

        restarted = restarted_module()
        tracker_rows = set()
        writer_calls = []

        def idempotent_tracker(_repo, item, *_args, **_kwargs):
            key = (q.pr_number_of(item), q._canonical_queue_identity(item))
            writer_calls.append(key)
            if key in tracker_rows:
                return "present"
            tracker_rows.add(key)
            return "synthetic-commit"

        real_write = restarted._write_queue_unlocked
        interrupted = [False]

        def stop_before_sync_ack(path, rows):
            if (not interrupted[0]
                    and any(row.get("tracker_sync") == "synced" for row in rows)):
                interrupted[0] = True
                raise OSError("simulated stop after remote success")
            return real_write(path, rows)

        with mock.patch.object(restarted, "write_tracker_row", side_effect=idempotent_tracker), \
                mock.patch.object(restarted, "_write_queue_unlocked",
                                  side_effect=stop_before_sync_ack):
            first_rc, first_summary, _ = capture_recovery(restarted, case)
        assert first_rc == 1
        assert first_summary["items"][0]["snapshot_provenance"] == "complete"
        assert first_summary["items"][0]["tracker_sync"] == "syncing"
        assert len(tracker_rows) == 1 and len(writer_calls) == 1

        with mock.patch.object(restarted, "write_tracker_row", side_effect=idempotent_tracker):
            second_rc, second_summary, second_text = capture_recovery(restarted, case)
            third_rc, _third_summary, _ = capture_recovery(restarted, case)
        repaired = restarted.load_queue(case["queue"])[0]
        assert second_rc == third_rc == 0
        assert second_summary["items"][0]["tracker_sync"] == "synced"
        assert repaired["status"] == "submitted"
        assert repaired["snapshot_provenance"] == "complete"
        assert repaired["tracker_sync_attempts"] == 2
        assert repaired["browser_owner_attempt_id"] == "attempt-12"
        assert repaired["runner_id"] == "runner-12"
        assert repaired["session_ref"] == "session-12"
        assert len(tracker_rows) == 1 and len(writer_calls) == 2
        assert "PDF and answers" not in second_text
        assert "Employer portal confirmed receipt." not in second_text


# Old public rows can have applied_date and no attempt or snapshot linkage.
# Recovery preserves their terminal truth, records missing artifacts, and does
# not manufacture either attempt identity or prepared bytes.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="submitted")
    row = q.load_queue(case["queue"])[0]
    row.update({"status": "submitted", "applied_date": "2026-11-16",
                "sent": "legacy public submission summary"})
    row.pop("submitted_at", None)
    row.pop("attempt_id", None)
    row.pop("submitted_attempt_id", None)
    q.write_queue(case["queue"], [row])

    with mock.patch.object(q, "write_tracker_row", return_value="") as writer:
        rc, summary, text = capture_recovery(q, case)
    failed_row = q.load_queue(case["queue"])[0]
    assert rc == 1 and writer.call_count == 1
    assert failed_row["status"] == "submitted"
    assert failed_row["snapshot_provenance"] == "unknown"
    assert failed_row["snapshot_error_code"] == "snapshot_missing_legacy"
    assert not failed_row.get("snapshot_id") and not failed_row.get("prepared_snapshot_id")
    assert not failed_row.get("attempt_id") and not failed_row.get("submitted_attempt_id")
    assert failed_row["submitted_at"] == "2026-11-16"
    assert failed_row["tracker_sync"] == "error"
    assert failed_row["tracker_sync_attempts"] == 1
    assert failed_row["tracker_sync_last_error"] == "tracker_write_failed"
    assert summary["snapshot_missing"] == 1
    assert summary["items"][0]["tracker_sync_pending_age_seconds"] is not None
    assert "legacy public submission summary" not in text
    assert "Fixture Co" not in text

    with mock.patch.object(q, "write_tracker_row", return_value="present") as writer:
        assert capture_recovery(q, case)[0] == 0
        assert writer.call_count == 1
    repaired = q.load_queue(case["queue"])[0]
    assert repaired["status"] == "submitted" and repaired["tracker_sync"] == "synced"
    assert repaired["tracker_sync_attempts"] == 2
    assert repaired["tracker_sync_last_error"] == "tracker_write_failed"


# A tracker identity conflict remains visible for review and does not alter the
# durable local confirmation.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="submitted")
    row = q.load_queue(case["queue"])[0]
    row.update({"submitted_at": "2026-11-16", "sent": "PDF"})
    q.write_queue(case["queue"], [row])
    with mock.patch.object(q, "write_tracker_row", return_value=q.TRACKER_CONFLICT) as writer:
        rc, summary, _ = capture_recovery(q, case)
    conflicted = q.load_queue(case["queue"])[0]
    assert rc == 1 and writer.call_count == 1
    assert conflicted["status"] == "submitted"
    assert conflicted["tracker_sync"] == "error"
    assert conflicted["tracker_sync_error"] == "tracker_identity_conflict"
    assert summary["tracker_conflicts"] == 1


# Concurrent scheduled and manual retries share a writer lock. The second
# caller observes the first durable success and does not issue a duplicate.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="submitted")
    row = q.load_queue(case["queue"])[0]
    row.update({"attempt_id": "attempt-12", "submitted_attempt_id": "attempt-12",
                "submitted_at": "2026-11-16", "sent": "PDF"})
    q.write_queue(case["queue"], [row])
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_tracker(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5), "tracker writer was not released"
        return "synthetic-commit"

    def run_sync():
        return q.sync_tracker_attempt(case["queue"], 12, case["repo"], lambda *_: None)

    with mock.patch.object(q, "write_tracker_row", side_effect=blocked_tracker):
        first = threading.Thread(target=run_sync)
        second = threading.Thread(target=run_sync)
        first.start()
        assert entered.wait(5), "first tracker writer did not start"
        second.start()
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    synced = q.load_queue(case["queue"])[0]
    assert synced["tracker_sync"] == "synced"
    assert synced["tracker_sync_attempts"] == 1


# If the queue identity changes during external I/O, the old result cannot
# mark that row synced even though its submitted attempt remains terminal.
with tempfile.TemporaryDirectory() as directory:
    case = make_case(directory, status="submitted")
    row = q.load_queue(case["queue"])[0]
    row.update({"attempt_id": "attempt-12", "submitted_attempt_id": "attempt-12",
                "submitted_at": "2026-11-16", "sent": "PDF"})
    q.write_queue(case["queue"], [row])

    def mutate_identity_during_tracker(_repo, _item, *_args, **_kwargs):
        with q.queue_lock(case["queue"]):
            latest = q.load_queue(case["queue"])
            latest[0]["link"] = "https://fixture.invalid/jobs/99"
            q._write_queue_unlocked(case["queue"], latest)
        return "synthetic-commit"

    with mock.patch.object(q, "write_tracker_row",
                           side_effect=mutate_identity_during_tracker):
        rc = q.sync_tracker_attempt(case["queue"], 12, case["repo"], lambda *_: None)
    stale = q.load_queue(case["queue"])[0]
    assert rc == 1
    assert stale["status"] == "submitted"
    assert stale["link"] == "https://fixture.invalid/jobs/99"
    assert stale["tracker_sync"] == "syncing"

print("test_queue_recovery: all checks passed")
