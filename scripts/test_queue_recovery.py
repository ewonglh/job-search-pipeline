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

print("test_queue_recovery: all checks passed")
