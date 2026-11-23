#!/usr/bin/env python3
"""Offline self-check for queue.py: store round-trip, idempotent enqueue, drain.

No network, no gh, no repo writes. Run: python3 test_queue.py
"""
import contextlib
import io
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
from unittest import mock

import queue as q

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "queue")

with open(os.path.join(FIX, "merged_prs.json"), encoding="utf-8") as fh:
    PRS = json.load(fh)
with open(os.path.join(FIX, "applications_merged.yaml"), encoding="utf-8") as fh:
    TRACKER = fh.read()

GIT = ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
       "-c", "commit.gpgsign=false"]


def git(repo, *args):
    done = subprocess.run(GIT + ["-C", repo, *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def remove_readonly(func, path, exc_info):
    os.chmod(path, 0o700)
    func(path)


def make_remote(root, tracker, name="resume"):
    """Local synthetic origin plus a clone of it holding ``tracker`` on main."""
    origin = os.path.join(root, name + "-origin.git")
    clone = os.path.join(root, name)
    subprocess.run(GIT + ["init", "--bare", "-q", "-b", "main", origin], check=True)
    subprocess.run(GIT + ["init", "-q", "-b", "main", clone], check=True)
    git(clone, "remote", "add", "origin", origin)
    publish_tracker(clone, tracker)
    return clone


def publish_tracker(clone, text):
    """Commit a new tracker on the clone's main and push it to origin."""
    with open(os.path.join(clone, q.TRACKER_PATH), "w", encoding="utf-8") as fh:
        fh.write(text)
    git(clone, "add", q.TRACKER_PATH)
    git(clone, "commit", "-q", "-m", "tracker")
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD")


def other_lane(clone, root):
    """A second clone of the same origin, standing in for another lane."""
    other = os.path.join(root, "other-lane")
    if not os.path.isdir(other):
        subprocess.run(GIT + ["clone", "-q", "--branch", "main", git(clone, "remote", "get-url", "origin"),
                              other], check=True, capture_output=True)
    git(other, "pull", "-q", "--ff-only", "origin", "main")
    return other


def live_enqueue(queue_path, repo, prs=PRS, extra=()):
    # Inject the authenticated GitHub boundary, never a writable fixture CLI
    # flag. The tracker is read through the real refresh from a local origin.
    # Real calls still require the observed owner's merge.
    with mock.patch.object(q, "gh_merged_prs", return_value=prs), \
            mock.patch.object(q, "GH_REPO", "demo-user/resume"):
        return q.main(["enqueue", "--queue", queue_path, "--repo", repo, *extra])


def quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return func(*args, **kwargs)


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()

# ---------------------------------------------------------------- yaml subset
obj = [{"company": "Acme: Inc", "role": "SWE #1", "notes": "x | y",
        "pr_number": 12, "flag": True}]
assert q.parse_yaml_list(q.dump_yaml_list(obj)) == obj, q.dump_yaml_list(obj)
assert q.parse_yaml_list("[]") == []
assert q.parse_yaml_list(q.dump_queue([])) == []
assert q.parse_yaml_list(q.dump_queue(obj)) == obj

# the header comment must survive as a comment, not data
dumped = q.dump_queue(obj)
assert dumped.startswith("# Application queue")
assert q.parse_yaml_list(dumped)[0]["company"] == "Acme: Inc"

# ---------------------------------------------------------------- helpers
assert q.norm_ts("2026-10-06T04:57:32Z") == "2026-10-06T04:57:32Z"
assert q.norm_ts("2026-10-06T12:57:32+08:00") == "2026-10-06T04:57:32Z"
assert q.norm_ts("") == ""
assert q.norm_ts("nope") == ""

assert q.canonical_submitted("Applied") and q.canonical_submitted("interviewing")
assert q.canonical_submitted("withdrawn") and not q.canonical_submitted("Tailored")

assert q.variant_from_files([{"path": "applications.yaml"},
                             {"path": "variants/acme-swe/resume.tex"}]) \
    == "variants/acme-swe/resume.tex"
assert q.variant_from_files([{"path": "variants/acme-swe.tex"}]) \
    == "variants/acme-swe.tex"
assert q.variant_from_files([{"path": "variants/acme-swe/notes.md"}]) is None
assert q.is_tailoring_pr([{"path": "variants/acme-swe/resume.tex"}])
assert not q.is_tailoring_pr([{"path": "applications.yaml"}])

# the built PDF that must be shown to the owner before a submission
assert q.variant_pdf("variants/acme-swe/resume.tex") == "variants/acme-swe/resume.pdf"
assert q.variant_pdf("variants/acme-swe.tex") == "variants/acme-swe.pdf"
assert q.variant_pdf("variants/acme-swe/resume.pdf") == "variants/acme-swe/resume.pdf"

# A claim cannot be created through generic status changes. Only a durable
# claim command grants an attempt ID that can record a submission.
assert "approved" not in q.STATUSES
assert q.TRANSITIONS["in_progress"] == {"submitted"}
assert q.TRANSITIONS["queued"] == {"failed"}
assert "submitted" not in q.TRANSITIONS["queued"]
assert q.UNFINISHED == ("queued", "in_progress")

c, r = q.title_parts({"title": "app(lumen-ai): tailored 1-page resume variant "
                               "\u2014 Founding AI / Full-stack Engineer Intern"})
assert c == "Lumen Ai", c
assert r == "Founding AI / Full-stack Engineer Intern", r

# ---------------------------------------------------------------- enqueue: empty
items, added, skipped = q.enqueue([], [dict(p, _tracker=TRACKER) for p in PRS])
assert [it["pr_number"] for it in added] == [12, 14], added
assert [it["enqueued_at"] for it in added] == \
    ["2026-10-06T06:00:00Z", "2026-10-06T07:30:00Z"], added
q12 = added[0]
assert q12["company"] == "Quantum Co" and q12["role"] == "Software Engineer Intern"
assert q12["source"] == "LinkedIn" and q12["link"] == "https://example.com/jobs/quantum"
assert q12["variant"] == "variants/quantum-co-swe/resume.tex"
assert q12["pr"] == "https://github.com/demo-user/resume/pull/12"
assert q12["status"] == "queued"
# the merge stamped the per-CV approval, with no human input
assert q12["approved_at"] == "2026-10-06T06:00:00Z", q12
assert q12["approved_by"] == "demo-user", q12
q14 = added[1]
assert q14["company"] == "Ghost Co" and q14["source"] == "InternSG"
assert q14["pr_number"] == 14
assert q14["approved_at"] == "2026-10-06T07:30:00Z", q14
assert q14["approved_by"] == "demo-user", q14
reasons = dict(skipped)
assert reasons[56] == "not an app/* PR", skipped
assert reasons[9] == "already submitted in tracker", skipped
assert reasons[13] == "not a tailoring PR (no variants/**/resume.tex)", skipped

# ---------------------------------------------------------------- idempotency
existing = q.load_queue(os.path.join(FIX, "queue_existing.yaml"))
assert [it["pr_number"] for it in existing] == [12]
items2, added2, skipped2 = q.enqueue(existing, [dict(p, _tracker=TRACKER) for p in PRS])
assert [it["pr_number"] for it in added2] == [14], added2
assert dict(skipped2)[12] == "already queued", skipped2
assert [it["pr_number"] for it in items2] == [12, 14]

# a second pass over the result adds nothing
items3, added3, _ = q.enqueue(items2, [dict(p, _tracker=TRACKER) for p in PRS])
assert added3 == [] and [it["pr_number"] for it in items3] == [12, 14]

# ---------------------------------------------------------------- submitted opt-in
_, added_s, _ = q.enqueue([], [dict(p, _tracker=TRACKER) for p in PRS],
                          include_submitted=True)
assert [it["pr_number"] for it in added_s] == [9, 12, 14], added_s
assert added_s[0]["status"] == "submitted"
assert added_s[0]["submitted_at"] == "2026-10-06"
assert added_s[0]["approved_at"] == "2026-10-06T04:57:32Z", added_s[0]
assert added_s[0]["approved_by"] == "demo-user", added_s[0]

# ---------------------------------------------------------------- active guard
prA = {"number": 20, "title": "app(dup-co): tailored 1-page resume variant \u2014 Swe",
       "headRefName": "app/dup-co", "mergedAt": "2026-10-06T09:00:00Z",
       "url": "https://github.com/demo-user/resume/pull/20",
       "mergeCommit": {"oid": "o20"}, "mergedBy": {"login": "demo-user"},
       "files": [{"path": "variants/dup-co/resume.tex"}], "_tracker": None}
prB = dict(prA, number=21, url="https://github.com/demo-user/resume/pull/21",
           mergedAt="2026-10-06T09:30:00Z",
           files=[{"path": "variants/dup-co-2/resume.tex"}])
_, added_d, skipped_d = q.enqueue([], [prA, prB])
assert [it["pr_number"] for it in added_d] == [20], added_d
assert dict(skipped_d)[21] == "already active for this application", skipped_d

# ---------------------------------------------------------------- slug misattribution
# A tracker row is this PR's own only when its pr/pr_number agrees. An older
# submission at the same company must not mark a later PR as already handled:
# that skip kept the PR out of queue.yaml, so the posting looked untailored and
# tailor.py opened a duplicate PR for it.
TRACKER_TWO_ROWS = """
- company: Orion Games
  role: AI Engineer Intern (Model & Product Evaluation)
  link: https://www.linkedin.com/jobs/view/3999000001
  status: applied
  applied_date: 2026-10-20
  variant: variants/orion-games-ai-engineer-intern/resume.tex
  pr: https://github.com/demo-user/resume/pull/196
  pr_number: 196
"""
orion_pr = {
    "number": 272, "title": "Tailor resume: Orion Games — Intern, Game Programmer",
    "headRefName": "app/orion-games", "mergedAt": "2026-11-02T15:32:44Z",
    "url": "https://github.com/demo-user/resume/pull/272",
    "mergedBy": {"login": "demo-user"},
    "body": "https://www.linkedin.com/jobs/view/3999000002",
    "files": [{"path": "variants/orion-games-game-programmer-intern/resume.tex"}],
}
assert q.extract_entry(orion_pr, TRACKER_TWO_ROWS) is None, \
    "a company-slug match must not claim another PR's tracker row"
_, g_added, g_skipped = q.enqueue([], [dict(orion_pr, _tracker=TRACKER_TWO_ROWS)])
assert [it["pr_number"] for it in g_added] == [272], (g_added, g_skipped)
assert g_skipped == [], g_skipped
# A second PR carrying the already submitted posting is a different case.
duplicate = dict(orion_pr, body="https://www.linkedin.com/jobs/view/3999000001")
_, duplicate_rows, duplicate_skips = q.enqueue([], [dict(duplicate, _tracker=TRACKER_TWO_ROWS)])
assert not duplicate_rows and duplicate_skips == [(272, "already submitted in tracker")]
# the row that really is this PR's still skips (same PR, submitted)
same = dict(orion_pr, number=196, url="https://github.com/demo-user/resume/pull/196",
            headRefName="app/orion-games-ai-engineer-intern")
_, s_added, s_skipped = q.enqueue([], [dict(same, _tracker=TRACKER_TWO_ROWS)])
assert s_added == [] and dict(s_skipped)[196] == "already submitted in tracker", s_skipped
# identity: URL wins when present, pr_number when only that is set, blank = this PR
assert q.entry_matches_pr({"pr": orion_pr["url"] + "/"}, orion_pr)
assert not q.entry_matches_pr({"pr": "https://github.com/demo-user/resume/pull/196"},
                               orion_pr)
assert q.entry_matches_pr({"pr_number": 272}, orion_pr)
assert not q.entry_matches_pr({"pr_number": 196}, orion_pr)
assert q.entry_matches_pr({"company": "X"}, orion_pr)
assert q.entry_matches_pr({"pr_number": 272}, orion_pr), \
    "a pr_number that agrees identifies this PR"
assert not q.entry_matches_pr({"pr_number": 196}, orion_pr)
assert q.entry_pr_number({"pr": "https://github.com/demo-user/resume/pull/7"}) == 7

# ---------------------------------------------------------------- next / sort
assert q.pr_number_of({"pr_number": 5}) == 5
assert q.pr_number_of({"pr": "https://github.com/demo-user/resume/pull/7"}) == 7
ordered = q.sort_items([{"enqueued_at": "2026-10-06T07:00:00Z", "pr_number": 2},
                        {"enqueued_at": "2026-10-06T06:00:00Z", "pr_number": 9}])
assert [it["pr_number"] for it in ordered] == [9, 2]

# ---------------------------------------------------------------- cli e2e
tmp = tempfile.mkdtemp()
original_snapshot_root = q.SNAPSHOT_ROOT
try:
    q.SNAPSHOT_ROOT = os.path.join(tmp, "private-snapshots")
    qfile = os.path.join(tmp, "queue.yaml")
    repo = make_remote(tmp, TRACKER)

    # A missing queue is a source error, never an empty queue. Every command
    # that would read or write it stops, names the init command, and creates
    # nothing.
    for argv in (["list"], ["next"], ["release-stale"],
                 ["status", "--pr", "12", "--status", "failed", "--notes", "x"],
                 ["claim", "--pr", "12", "--repo", repo,
                  "--runner-id", "queue-test-runner", "--session-ref", "queue-test-session"],
                 ["enqueue", "--repo", repo],
                 ["enqueue", "--dry-run", "--repo", repo]):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {
                    "JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": shlex.quote(sys.executable)}):
            with mock.patch.object(q, "gh_merged_prs", return_value=PRS) as gh:
                rc = q.main([argv[0], "--queue", qfile] + argv[1:])
        assert rc in (1, 2), (argv, rc)
        assert "queue.py init --queue" in err.getvalue(), (argv, err.getvalue())
        assert not os.path.exists(qfile), argv
        gh.assert_not_called()
    try:
        q.load_queue(qfile)
        raise AssertionError("missing queue loaded")
    except q.QueueMissingError as exc:
        assert "queue.py init --queue" in str(exc)

    # Offline metadata cannot mint authorization or write even an empty store.
    for flags in (["--prs", os.path.join(FIX, "merged_prs.json")],
                  ["--tracker", os.path.join(FIX, "applications_merged.yaml")]):
        assert q.main(["enqueue", "--queue", qfile] + flags) == 2
        assert not os.path.exists(qfile)

    # init creates the empty store once and never overwrites a valid one.
    assert quiet(q.main, ["init", "--queue", qfile]) == 0
    assert q.load_queue(qfile) == []
    created = read_bytes(qfile)
    assert quiet(q.main, ["init", "--queue", qfile]) == 0
    assert read_bytes(qfile) == created

    # dry-run: adds to stdout, writes nothing
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = q.main(["enqueue", "--prs", os.path.join(FIX, "merged_prs.json"),
                     "--tracker", os.path.join(FIX, "applications_merged.yaml"),
                     "--queue", qfile, "--dry-run"])
    assert rc == 0
    assert read_bytes(qfile) == created, "--dry-run must not write the store"
    out = buf.getvalue()
    assert out.count("queued:") == 2, repr(out)
    assert "Quantum Co" in out and "Ghost Co" in out, out

    # A non-owner merge and missing merge time are not enqueue authorization.
    for bad in ([dict(pr, mergedBy={"login": "untrusted-fixture"}) for pr in PRS],
                [dict(pr, mergedAt=None) for pr in PRS]):
        assert live_enqueue(qfile, repo, bad) == 0
        assert q.load_queue(qfile) == []

    # real run writes the store
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live_enqueue(qfile, repo)
    assert rc == 0 and os.path.exists(qfile)
    store = q.load_queue(qfile)
    assert [it["pr_number"] for it in store] == [12, 14], store
    # the temporary fetch ref never outlives the call
    assert git(repo, "for-each-ref", "refs/jobsearch") == ""

    # re-run: silent (nothing new) but exit 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live_enqueue(qfile, repo)
    assert rc == 0 and buf.getvalue() == "", repr(buf.getvalue())

    # next -> oldest queued (PR 12)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = q.main(["next", "--queue", qfile, "--json"])
    assert rc == 0
    nxt = json.loads(buf.getvalue())
    assert nxt["pr_number"] == 12 and nxt["company"] == "Quantum Co", nxt

    # Status cannot claim. Seed the exact durable marker that `claim` writes;
    # the dedicated eligibility suite exercises that command.
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "in_progress"]) == 2
    claimed_items = q.load_queue(qfile)
    claimed_idx = next(i for i, row in enumerate(claimed_items)
                       if row["pr_number"] == 12)
    claimed_items[claimed_idx].update({
        "status": "in_progress", "attempt_id": "synthetic-attempt-12",
        "attempt_state": "authorized", "submit_gate": "durable",
        "claimed_at": q.now_utc()})
    claimed_items[claimed_idx]["claim_application_id"] = q._canonical_queue_identity(
        claimed_items[claimed_idx])
    q.write_queue(qfile, claimed_items)
    # next never skips past the in-flight item, and names the CV to submit
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        q.main(["next", "--queue", qfile, "--json"])
    nxt = json.loads(buf.getvalue())
    assert nxt["pr_number"] == 12, nxt
    assert nxt["variant"] == "variants/quantum-co-swe/resume.tex", nxt
    assert nxt["pdf"] == "variants/quantum-co-swe/resume.pdf", nxt
    assert nxt["pr"].endswith("/pull/12"), nxt
    assert nxt["approved_by"] == "demo-user", nxt
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        q.main(["next", "--queue", qfile])
    assert "pdf      variants/quantum-co-swe/resume.pdf" in buf.getvalue()
    assert "merge approval by demo-user" in buf.getvalue(), buf.getvalue()
    # an undocumented submission is still refused: no date and no record
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "submitted"]) == 2
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "submitted", "--date", "2026-10-06"]) == 2
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "submitted", "--sent", "PDF via company ATS"]) == 2
    # Confirmation must match the durable attempt, even with concrete evidence.
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "submitted", "--date", "2026-10-06",
                   "--sent", "PDF via company ATS; availability Jun-Dec 2027",
                   "--attempt-id", "another-attempt-12",
                   "--confirmation-evidence", "Employer portal confirmed receipt.",
                   "--no-tracker"]) == 2
    pdf_file = os.path.join(tmp, "selected-resume.pdf")
    answers_file = os.path.join(tmp, "selected-answers.json")
    synthetic_repo = os.path.join(tmp, "resume-repo")
    with open(pdf_file, "wb") as stream:
        stream.write(b"%PDF-1.7\nSynthetic resume\n")
    with open(answers_file, "w", encoding="utf-8") as stream:
        stream.write('{"availability":"June to December 2027"}\n')
    assert q.main(["prepare-snapshot", "--queue", qfile, "--repo", synthetic_repo,
                   "--pr", "12",
                   "--attempt-id", "synthetic-attempt-12", "--pdf", pdf_file,
                   "--answers", answers_file, "--operator-verification",
                   "I checked the selected PDF and answers for this posting."]) == 0
    assert q.main(["verify-snapshot", "--queue", qfile, "--repo", synthetic_repo,
                   "--pr", "12",
                   "--attempt-id", "synthetic-attempt-12", "--pdf", pdf_file,
                   "--answers", answers_file]) == 0
    # The attempt ID, confirmation, date, and sent record are mandatory.
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "submitted", "--date", "2026-10-06",
                   "--sent", "PDF via company ATS; availability Jun-Dec 2027",
                   "--route", "careers.example.com",
                   "--attempt-id", "synthetic-attempt-12",
                   "--confirmation-evidence", "Employer portal confirmed receipt.",
                   "--no-tracker"]) == 0
    it12 = [it for it in q.load_queue(qfile) if it["pr_number"] == 12][0]
    assert it12["status"] == "submitted"
    assert it12["submitted_at"] == "2026-10-06"
    assert it12["sent"].startswith("PDF via company ATS")
    assert it12["approved_at"] == "2026-10-06T06:00:00Z"
    assert it12["approved_by"] == "demo-user"
    assert it12["route"] == "careers.example.com"
    assert it12["tracker_sync"] == "pending"

    # submitted is terminal
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "in_progress"]) == 2
    assert q.main(["status", "--queue", qfile, "--pr", "12",
                   "--status", "queued", "--force"]) == 2

    # next -> PR 14 now (12 is submitted)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        q.main(["next", "--queue", qfile, "--json"])
    assert json.loads(buf.getvalue())["pr_number"] == 14

    # A pre-claim failure is terminal; generic notes and --force cannot reopen it.
    assert q.main(["status", "--queue", qfile, "--pr", "14",
                   "--status", "failed"]) == 2
    assert q.main(["status", "--queue", qfile, "--pr", "14",
                   "--status", "failed", "--notes", "posting closed"]) == 0
    assert q.main(["status", "--queue", qfile, "--pr", "14",
                   "--status", "queued", "--force"]) == 2

    # list --status filtering
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        q.main(["list", "--queue", qfile, "--json", "--status", "submitted"])
    assert [it["pr_number"] for it in json.loads(buf.getvalue())] == [12]

    # initialised empty queue -> next is silent
    empty = os.path.join(tmp, "empty.yaml")
    assert quiet(q.main, ["init", "--queue", empty]) == 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = q.main(["next", "--queue", empty])
    assert rc == 0 and buf.getvalue() == "", repr(buf.getvalue())
finally:
    q.SNAPSHOT_ROOT = original_snapshot_root
    shutil.rmtree(tmp, onerror=remove_readonly)


# ---------------------------------------------------------------- queue formats
# Valid empty and valid legacy stores load; malformed stores stop every write
# and are left byte-for-byte intact. None of these is "missing".
with tempfile.TemporaryDirectory() as root:
    repo = make_remote(root, TRACKER)
    path = os.path.join(root, "queue.yaml")
    assert quiet(q.main, ["init", "--queue", path]) == 0
    for text in ("", "# a truncated tracker\n"):
        revision = q.TrackerRevision("a" * 40, text, q.hashlib.sha256(text.encode()).hexdigest())
        with mock.patch.object(q, "gh_merged_prs", return_value=PRS), \
                mock.patch.object(q, "refresh_main_tracker", return_value=revision), \
                mock.patch.object(q, "gh_pr_files") as metadata:
            assert quiet(q.main, ["enqueue", "--repo", repo, "--queue", path]) != 0
            assert q.load_queue(path) == []
            metadata.assert_not_called()
    for label, text in (("explicit empty", "[]\n"),
                        ("generated empty", q.dump_queue([])),
                        ("comment-only header then empty", "# header\n\n[]\n")):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        assert q.load_queue(path) == [], label
    legacy = q.load_queue(os.path.join(FIX, "queue_existing.yaml"))
    assert [row["pr_number"] for row in legacy] == [12]
    shutil.copy(os.path.join(FIX, "queue_existing.yaml"), path)
    assert quiet(q.main, ["init", "--queue", path]) == 0, "init keeps a valid legacy queue"
    assert q.load_queue(path) == legacy
    assert quiet(q.main, ["list", "--queue", path]) == 0

    for label, text in (("zero bytes", ""), ("prose", "not a list\n"),
                        ("unknown status", "- pr_number: 3\n  status: bogus\n"),
                        ("duplicate PR", "- pr_number: 3\n- pr_number: 3\n"),
                        ("empty marker plus rows", "[]\n- pr_number: 3\n")):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        before = read_bytes(path)
        try:
            q.load_queue(path)
            raise AssertionError(label + " loaded")
        except q.QueueMissingError:
            raise AssertionError(label + " reported as missing")
        except q.QueueStateError:
            pass
        with mock.patch.object(q, "gh_merged_prs", return_value=PRS):
            assert quiet(q.main, ["enqueue", "--queue", path, "--repo", repo]) == 1, label
        assert quiet(q.main, ["init", "--queue", path]) == 2, label
        with mock.patch.dict(os.environ, {
                "JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER": shlex.quote(sys.executable)}):
            assert quiet(q.main, ["claim", "--queue", path, "--repo", repo, "--pr", "3",
                                  "--runner-id", "queue-test-runner",
                                  "--session-ref", "queue-test-session"]) == 2, label
        assert read_bytes(path) == before, label


# ---------------------------------------------------------------- fresh tracker revision
# The tracker comes from the commit that a successful origin/main fetch
# returned: not the working tree, not a stale origin/main ref.
with tempfile.TemporaryDirectory() as root:
    repo = make_remote(root, "[]\n")
    cached_head = git(repo, "rev-parse", "origin/main")
    lane = other_lane(repo, root)
    submitted = TRACKER
    new_head = publish_tracker(lane, submitted)
    # The clone knows nothing of the new commit; its working tree is stale too.
    assert git(repo, "rev-parse", "origin/main") == cached_head
    revision = q.refresh_main_tracker(repo)
    assert revision.commit == new_head and revision.text == submitted
    assert revision.sha256 == __import__("hashlib").sha256(submitted.encode()).hexdigest()
    assert git(repo, "rev-parse", "origin/main") == new_head
    assert git(repo, "for-each-ref", "refs/jobsearch") == ""
    q.verify_tracker_revision(repo, revision)

    # Enqueue skips PR 9 because the fresh tracker shows it submitted, even
    # though the clone's own cached copy and working tree have no such row.
    path = os.path.join(root, "queue.yaml")
    assert quiet(q.main, ["init", "--queue", path]) == 0
    with open(os.path.join(repo, q.TRACKER_PATH), "w", encoding="utf-8") as fh:
        fh.write("[]\n")
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        assert live_enqueue(path, repo, extra=("--json",)) == 0
    assert "skip #9: already submitted in tracker" in err.getvalue(), err.getvalue()
    assert "pinned to origin/main " + new_head[:12] in err.getvalue()


# ---------------------------------------------------------------- failed refresh, cached data
# A readable cached copy must not stand in for a refresh that failed: enqueue
# stops before any PR file lookup or write, and says what to do.
with tempfile.TemporaryDirectory() as root:
    repo = make_remote(root, TRACKER)
    path = os.path.join(root, "queue.yaml")
    assert quiet(q.main, ["init", "--queue", path]) == 0
    before = read_bytes(path)
    assert git(repo, "show", "origin/main:" + q.TRACKER_PATH) == TRACKER.strip()  # cache is readable
    git(repo, "remote", "set-url", "origin",
        "file://" + os.path.join(root, "gone-remote.git"))
    assert "secret" not in q._redact(
        "fatal: unable to access 'https://user:secret@host.example/x.git/': no route")
    prs = [dict(pr, files=None) for pr in PRS]
    for dry in ((), ("--dry-run",)):
        err = io.StringIO()
        with mock.patch.object(q, "gh_pr_files") as files, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            assert live_enqueue(path, repo, prs, extra=dry) == 1
        files.assert_not_called()
        assert "tracker refresh failed" in err.getvalue(), err.getvalue()
        assert "cached data was not used" in err.getvalue()
        assert "secret" not in err.getvalue(), "credentials must not reach logs"
        assert read_bytes(path) == before
    try:
        q.refresh_main_tracker(repo)
        raise AssertionError("refresh succeeded against an unreachable remote")
    except q.TrackerRefreshError:
        pass
    assert git(repo, "for-each-ref", "refs/jobsearch") == ""
    # No clone at all is also a refresh failure, not an empty tracker.
    try:
        q.refresh_main_tracker(os.path.join(root, "not-a-clone"))
        raise AssertionError("refresh succeeded without a clone")
    except q.TrackerRefreshError:
        pass


# ---------------------------------------------------------------- pinned recheck under lock
# Another lane lands a newer tracker after our refresh but before our write.
# The locked recheck must refuse the write rather than trust the old revision.
with tempfile.TemporaryDirectory() as root:
    repo = make_remote(root, TRACKER)
    lane = other_lane(repo, root)
    path = os.path.join(root, "queue.yaml")
    assert quiet(q.main, ["init", "--queue", path]) == 0
    before = read_bytes(path)
    real_refresh = q.refresh_main_tracker
    races = []

    def refresh_then_race(clone, log=lambda *a: None, **kw):
        revision = real_refresh(clone, log, **kw)
        races.append(1)
        publish_tracker(lane, TRACKER + "- company: Racer %d\n  role: Intern\n"
                        "  link: https://example.com/jobs/racer-%d\n  status: applied\n"
                        % (len(races), len(races)))
        git(clone, "fetch", "-q", "origin", "main")  # another lane's fetch
        return revision

    err = io.StringIO()
    with mock.patch.object(q, "refresh_main_tracker", side_effect=refresh_then_race), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        assert live_enqueue(path, repo) == 1
    assert "tracker changed on origin/main after it was refreshed" in err.getvalue(), err.getvalue()
    assert read_bytes(path) == before
    # A dry run writes nothing, so it does not need the recheck.
    with mock.patch.object(q, "refresh_main_tracker", side_effect=refresh_then_race), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert live_enqueue(path, repo, extra=("--dry-run",)) == 0
    # The next run, with a fresh refresh, succeeds.
    assert live_enqueue(path, repo) == 0
    assert [row["pr_number"] for row in q.load_queue(path)] == [12, 14]


# ---------------------------------------------------------------- concurrent same-identity writes
# Two enqueues for different PRs of one application race through the lock:
# at most one active item results.
with tempfile.TemporaryDirectory() as root:
    repo = make_remote(root, "[]\n")
    path = os.path.join(root, "queue.yaml")
    assert quiet(q.main, ["init", "--queue", path]) == 0
    lists = {"first": [prA], "second": [prB]}
    gate = threading.Barrier(2)

    def per_thread_prs(log):
        gate.wait(timeout=20)
        return lists[threading.current_thread().name]

    results = {}

    def worker():
        results[threading.current_thread().name] = q.main(
            ["enqueue", "--queue", path, "--repo", repo])

    with mock.patch.object(q, "gh_merged_prs", side_effect=per_thread_prs), \
            mock.patch.object(q, "GH_REPO", "demo-user/resume"), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        threads = [threading.Thread(target=worker, name=name) for name in lists]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    assert results == {"first": 0, "second": 0}, results
    rows = q.load_queue(path)
    assert len(rows) == 1 and rows[0]["status"] == "queued", rows


# ---------------------------------------------------------------- tracker write-back
# Offline git exercise: a local "origin" bare repo + clone replaces GitHub, so
# the pull/append/commit/push/verify flow runs hermetically. Covers: the row
# lands on origin/main, the queue note records the commit, a second run is a
# no-op (idempotency guard), and a broken remote fails without corrupting the
# queue or raising.
_wtmp = tempfile.mkdtemp()
try:
    _origin = os.path.join(_wtmp, "origin.git")
    _clone = os.path.join(_wtmp, "clone")
    _git = ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com",
            "-c", "commit.gpgsign=false"]
    import subprocess as _sp
    _sp.run(_git + ["init", "--bare", "-q", "-b", "main", _origin], check=True)
    _sp.run(_git + ["clone", "-q", _origin, _clone], check=True)
    with open(os.path.join(_clone, q.TRACKER_PATH), "w", encoding="utf-8") as fh:
        fh.write("# tracker\n")
    def _g(*a):
        r = _sp.run(_git + ["-C", _clone] + list(a), capture_output=True, text=True)
        assert r.returncode == 0, (a, r.stderr)
        return r
    _g("add", q.TRACKER_PATH)
    _g("commit", "-q", "-m", "init")
    _g("branch", "-M", "main")
    _g("push", "-q", "origin", "main")

    def _origin_show():
        r = _sp.run(["git", "-C", _clone, "ls-remote", "origin", "main"],
                    capture_output=True, text=True)
        assert r.returncode == 0
        sha = r.stdout.split()[0]
        r2 = _sp.run(["git", "-C", _clone, "fetch", "-q", "origin", "main"],
                     capture_output=True, text=True)
        assert r2.returncode == 0
        r3 = _sp.run(["git", "-C", _clone, "show", sha + ":" + q.TRACKER_PATH],
                     capture_output=True, text=True)
        assert r3.returncode == 0
        return r3.stdout

    item = {"company": "Fixture Co", "role": "SWE Intern",
            "link": "https://fixture.invalid/jobs/12",
            "variant": "variants/fixture-co-swe/resume.tex",
            "pr": "https://github.com/demo-user/resume/pull/12", "pr_number": 12,
            "source": ""}
    sha1 = q.write_tracker_row(_clone, item, "2026-10-15", "PDF via ATS",
                               "careers.example.com", log=lambda *a: None)
    assert sha1, "write-back must report the pushed commit"
    text = _origin_show()
    assert "Fixture Co" in text and "applied" in text, text
    assert "pr_number: 12" in text, text

    # Second call for the same PR: idempotency guard, no duplicate row.
    sha2 = q.write_tracker_row(_clone, item, "2026-10-15", "PDF via ATS",
                               "careers.example.com", log=lambda *a: None)
    assert sha2 == "present", sha2
    text2 = _origin_show()
    assert text2.count("Fixture Co") == 1, text2

    # A PR alias cannot make a contradictory posting link look already synced.
    contradictory = dict(item, link="https://fixture.invalid/jobs/other")
    assert q.write_tracker_row(_clone, contradictory, "2026-10-15", "PDF via ATS",
                               "careers.example.com", log=lambda *a: None) \
        == q.TRACKER_CONFLICT

    # Broken remote (origin replaced by an empty dir): fails cleanly, no raise.
    _sp.run(["git", "-C", _clone, "remote", "set-url", "origin",
             os.path.join(_wtmp, "empty-dir")], check=True)
    os.makedirs(os.path.join(_wtmp, "empty-dir"), exist_ok=True)
    sha3 = q.write_tracker_row(_clone, item, "2026-10-15", "PDF via ATS",
                               "careers.example.com", log=lambda *a: None)
    assert sha3 == "", sha3

    # Cross-parser check: the row the writer produces must also parse under a
    # real YAML library with identical content (the Oct-12 quoting lessons).
    try:
        import yaml as _yaml
    except ImportError:
        _yaml = None
    if _yaml is not None:
        import datetime as _dt
        _r = q.build_tracker_row(item, "2026-10-15", "sent it; yes 'really' #tag",
                                 "careers.example.com", note="multi 'word' #hash: test")
        _t = q._append_tracker_row("# header\n", _r)
        parsed_lib = _yaml.safe_load(_t)
        # PyYAML types ISO dates as date objects; our minimal parser keeps
        # strings. Compare content with dates normalised.
        for e in parsed_lib:
            for k, v in list(e.items()):
                if isinstance(v, (_dt.date, _dt.datetime)):
                    e[k] = v.isoformat()[:10]
        parsed_own = [e for e in q.parse_yaml_list(_t) if isinstance(e, dict)]
        assert parsed_lib == parsed_own, (_t, parsed_lib, parsed_own)
finally:
    def _remove_readonly(func, path, exc_info):
        os.chmod(path, 0o700)
        func(path)
    shutil.rmtree(_wtmp, onerror=_remove_readonly)

print("test_queue: all assertions passed")
