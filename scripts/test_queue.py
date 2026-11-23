#!/usr/bin/env python3
"""Offline self-check for queue.py: store round-trip, idempotent enqueue, drain.

No network, no gh, no repo writes. Run: python3 test_queue.py
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import date
from unittest import mock

import queue as q
import tailor

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "queue")

with open(os.path.join(FIX, "merged_prs.json"), encoding="utf-8") as fh:
    PRS = json.load(fh)
with open(os.path.join(FIX, "applications_merged.yaml"), encoding="utf-8") as fh:
    TRACKER = fh.read()


def live_enqueue(queue_path, prs=PRS):
    # Inject the authenticated GitHub/read-main boundary, never a writable
    # fixture CLI flag. Real calls still require the observed owner's merge.
    root = os.path.dirname(queue_path)
    repo = os.path.join(root, "fixture-resume")
    os.makedirs(os.path.join(repo, "data"), exist_ok=True)
    postings = [
        {"company": "Quantum Co", "role": "Software Engineer Intern",
         "link": "https://example.com/jobs/quantum", "added": date.today().isoformat()},
        {"company": "Ghost Co", "role": "Backend Engineer Intern",
         "link": "https://www.internsg.com/job/ghost-co-backend",
         "added": date.today().isoformat()},
    ]
    with open(os.path.join(repo, "data", "postings.json"), "w", encoding="utf-8") as fh:
        json.dump(postings, fh)
    skipped = os.path.join(root, "skipped.yaml")
    gates = os.path.join(root, "gates.yaml")
    for path in (skipped, gates):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
    def observed(_log, on_page=None):
        if on_page:
            on_page(prs)
        return prs
    files_by_pr = {int(row.get("number") or 0): row.get("files") for row in prs}
    with mock.patch.object(q, "gh_merged_prs", side_effect=observed), \
            mock.patch.object(q, "gh_pr_files",
                              side_effect=lambda number, _log: files_by_pr.get(int(number))), \
            mock.patch.object(q, "read_main_tracker", return_value=TRACKER), \
            mock.patch.object(q, "GH_REPO", "demo-user/resume"), \
            mock.patch.object(q, "REPO_DIR", repo), \
            mock.patch.object(tailor, "SKIPPED_FILE", skipped), \
            mock.patch.object(tailor, "GATE_FILE", gates), \
            mock.patch.object(tailor, "fetch_jd", return_value={
                "jd_text": "Current role description. " * 20}):
        return q.main(["enqueue", "--queue", queue_path, "--repo", repo])

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
    "body": "https://www.linkedin.com/jobs/view/3999000001",
    "files": [{"path": "variants/orion-games-game-programmer-intern/resume.tex"}],
}
assert q.extract_entry(orion_pr, TRACKER_TWO_ROWS) is None, \
    "a company-slug match must not claim another PR's tracker row"
_, g_added, g_skipped = q.enqueue([], [dict(orion_pr, _tracker=TRACKER_TWO_ROWS)])
assert [it["pr_number"] for it in g_added] == [272], (g_added, g_skipped)
assert g_skipped == [], g_skipped
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
    # dry-run: adds to stdout, writes nothing
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = q.main(["enqueue", "--prs", os.path.join(FIX, "merged_prs.json"),
                     "--tracker", os.path.join(FIX, "applications_merged.yaml"),
                     "--queue", qfile, "--dry-run"])
    assert rc == 0
    assert not os.path.exists(qfile), "--dry-run must not write the store"
    out = buf.getvalue()
    assert out.count("queued:") == 2, repr(out)
    assert "Quantum Co" in out and "Ghost Co" in out, out

    # Offline metadata cannot mint authorization or write even an empty store.
    for flags in (["--prs", os.path.join(FIX, "merged_prs.json")],
                  ["--tracker", os.path.join(FIX, "applications_merged.yaml")]):
        assert q.main(["enqueue", "--queue", qfile] + flags) == 2
        assert not os.path.exists(qfile)
    # A non-owner merge and missing merge time are not enqueue authorization.
    for non_owner in (
            [dict(pr, mergedBy={"login": "untrusted-fixture"}) for pr in PRS],
            [dict(pr, mergedBy={"name": "demo-user"}) for pr in PRS]):
        assert live_enqueue(qfile, non_owner) == 0
        assert q.load_queue(qfile) == []
    missing_time = [dict(pr, mergedAt=None) for pr in PRS]
    assert live_enqueue(qfile, missing_time) == 1
    assert q.load_queue(qfile) == []
    pending = q.load_merge_intake(qfile)["records"]["12"]
    assert pending["state"] == "pending"
    assert pending["reason_code"] == "metadata_unavailable"

    # real run writes the store
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live_enqueue(qfile)
    assert rc == 0 and os.path.exists(qfile)
    store = q.load_queue(qfile)
    assert [it["pr_number"] for it in store] == [12, 14], store

    # Re-run: no addition, with a structured healthy-empty summary.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live_enqueue(qfile)
    assert rc == 0 and "queued:" not in buf.getvalue(), repr(buf.getvalue())
    assert '"outcome": "healthy_empty"' in buf.getvalue(), repr(buf.getvalue())

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

    # empty queue -> next is silent
    empty = os.path.join(tmp, "empty.yaml")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = q.main(["next", "--queue", empty])
    assert rc == 0 and buf.getvalue() == "", repr(buf.getvalue())
finally:
    q.SNAPSHOT_ROOT = original_snapshot_root
    shutil.rmtree(tmp)


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
    _sp.run(_git + ["init", "--bare", "-q", _origin], check=True)
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
