#!/usr/bin/env python3
"""Offline self-check for summary.py: parsing, buckets, new/ageing, digest, state.

No network, no repo writes. Run: python3 test_summary.py
"""
import contextlib
import datetime as dt
import io
import json
import os
import re as _re
import shutil
import tempfile

import summary as s

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "summary")
TODAY = dt.date(2026, 10, 6)
LAST = dt.date(2026, 10, 5)

with open(os.path.join(FIX, "applications.yaml"), encoding="utf-8") as fh:
    APPS = s.parse_yaml_list(fh.read())
with open(os.path.join(FIX, "postings.json"), encoding="utf-8") as fh:
    POSTINGS = json.load(fh)

# ---------------------------------------------------------------- status map
assert s.canonical_status("to_apply") == "Found"
assert s.canonical_status("to apply") == "Found"
assert s.canonical_status("Tailored") == "Tailored"
assert s.canonical_status("applied") == "Applied"
assert s.canonical_status("interviewing") == "Interview"
assert s.canonical_status("Offer") == "Accepted"
assert s.canonical_status("offer") == "Accepted"
assert s.canonical_status("signed") == "Accepted"
assert s.canonical_status("callback") == "Callback"
assert s.canonical_status("rfi") == "Callback"
assert s.canonical_status("screening") == "Callback"
assert s.canonical_status("oa") == "OA"
assert s.canonical_status("online_assessment") == "OA"
assert s.canonical_status("HackerRank") == "OA"
assert s.canonical_status("challenge") == "OA"
assert s.ORDER == ("Found", "Tailored", "Applied", "Callback", "OA",
                   "Interview", "Accepted", "Rejected")
assert s.canonical_status("withdrawn") == "Rejected"
assert s.canonical_status("REJECTED") == "Rejected"
assert s.canonical_status("") is None
assert s.canonical_status("nonsense") is None

# ---------------------------------------------------------------- date parse
assert s.parse_date("2026-10-06") == TODAY
assert s.parse_date("2026-10-06T21:00:00+08:00") == TODAY
assert s.parse_date("") is None
assert s.parse_date("nope") is None

# ---------------------------------------------------------------- fixtures
assert len(APPS) == 8, len(APPS)
assert s.parse_yaml_list("[]") == []

res = s.compute(APPS, POSTINGS, LAST, TODAY, 7)
assert res["counts"] == {"Found": 3, "Tailored": 1, "Applied": 2,
                         "Callback": 0, "OA": 0, "Interview": 1,
                         "Accepted": 1, "Rejected": 2}, res["counts"]
# Found = 1 tracker "to_apply" + 2 postings with no tracker entry (Acme dup excluded)
assert [a["company"] for a in res["new"]] == ["Acme Corp"], res["new"]
assert [a["company"] for a, _ in res["ageing"]] == ["Beta Pte Ltd"], res["ageing"]
assert res["ageing"][0][1] == 16
assert res["changed"] is True and res["silent"] is False

text = s.render_text(res)
# Option A: line 1 is the shared sender/lane preface, line 2 the human title.
assert text.splitlines()[0] == "jobs · summary · 06/10/2026", text
assert text.splitlines()[1] == "Daily digest · 06/10/2026", text
# contract patterns: identity line shape, and line 2 unindented with the
# exact ' · ' (U+00B7 + space) prefix the announce filter keys on.
assert _re.match(r"^(jobs|job-scout) · (summary|nudges|newpost) · "
                 r"\d\d/\d\d/\d\d\d\d$", text.splitlines()[0]), text
assert text.splitlines()[1].startswith("Daily digest · "), repr(text)
assert text.splitlines()[1][0] == "D", repr(text)  # column 0, never indented
# one merged counts line, then the section heading carrying its own count
assert text.splitlines()[2] == \
    "Applied 2 · Callbacks 0 · OA 0 · Interviews 1 · Offers 1 · " \
    "Rejections 2 · +1 applied, +2 found, 0 replies", text
assert "### Chase now · 1 · 14d or more" in text, text
assert "### New since 05/10/2026 · 1" in text, text
# one full entry line, verbatim: bold company anchor, role, age only
assert ("**<a href=\"https://example.com/jobs/beta\">Beta Pte Ltd</a>** · "
        "Software Engineer Intern · 16d") in text, text
assert "Acme Corp" in text and "Backend Engineer" in text, text
# the duplicate 'applied DD/MM' field and the separate '+N more' tail are gone
assert "applied 06/10" not in text and "applied 20/09" not in text, text
assert not any(ln.startswith("+") for ln in text.splitlines()), text
assert not any(ln.startswith("· ") for ln in text.splitlines()), text
assert "Found 3 · Tailored 1" in text, text
# the merged counts line (plus the Found/Tailored close) carries every bucket
_TALLY = {"Found": "Found", "Tailored": "Tailored", "Applied": "Applied",
          "Callback": "Callbacks", "OA": "OA",
          "Interview": "Interviews", "Accepted": "Offers",
          "Rejected": "Rejections"}
for bucket, n in res["counts"].items():
    assert f"{_TALLY[bucket]} {n}" in text, (bucket, text)

# Option A: no monospace tables, no em/en dashes, line budget holds
assert "```" not in text, text
assert not any(ch in text for ch in "\u2014\u2013"), text
assert text.count("\n") + 1 <= s.DIGEST_MAX_LINES, text
assert len(text) <= s.MESSAGE_CHAR_GUARD, len(text)
assert all(s.canonical_status(a["status"]) in s.SUBMITTED_STATUSES for a in res["new"])

# ---------------------------------------------------------------- tailor PRs
# Offline: the PR check must be mocked in every main() test below.
assert s.is_tailor_pr({"headRefName": "app/acme-backend", "title": "app(acme-backend): tailored 1-page resume variant — Backend"})
assert s.is_tailor_pr({"headRefName": "app/x", "title": "chore: sync"}) is False
assert s.is_tailor_pr({"headRefName": "feat/x", "title": "app(x): tailored variant"}) is False
assert s.is_tailor_pr({}) is False
assert s.slugify("ONE Tech Stop Pte Ltd") == "one-tech-stop-pte-ltd"
assert s.work_slug({"company": "Acme Corp", "role": "Backend Engineer Intern"}) == "acme-corp-backend-engineer-intern"
assert s.pr_branch_slug({"headRefName": "app/iota-inc-cloud-intern"}) == "iota-inc-cloud-intern"
assert "https://example.com/jobs/iota" in s.pr_body_links({"body": "see https://example.com/jobs/iota for the JD"})

# effective_tailor_prs: dedupes against tracker pr numbers, links and slugs
fake_prs = [
    {"number": 10, "headRefName": "app/iota-inc-cloud-intern", "title": "app(iota-inc-cloud-intern): tailored 1-page resume variant — Cloud Intern", "body": "posting https://example.com/jobs/iota"},
    {"number": 11, "headRefName": "app/kappa-pte-ltd-security-intern", "title": "app(kappa-pte-ltd-security-intern): tailored 1-page resume variant — Security", "body": "no link here"},
    {"number": 12, "headRefName": "app/acme-corp-backend-engineer-intern", "title": "app(acme-corp-backend-engineer-intern): tailored 1-page resume variant — Backend", "body": ""},
]
kept = s.effective_tailor_prs(fake_prs, APPS)
assert [p["number"] for p in kept] == [10, 11], kept  # 12 dedupes: tracker Acme link+slug
# explicit tracker pr field also dedupes
apps_pr = APPS + [{"company": "Iota Inc", "role": "Cloud Intern", "link": "https://example.com/jobs/iota", "status": "Tailored", "pr": "https://github.com/demo-user/resume/pull/10"}]
assert [p["number"] for p in s.effective_tailor_prs(fake_prs, apps_pr)] == [11]

# compute with PRs: postings matched to open tailor PRs move Found -> Tailored
res_pr = s.compute(APPS, [p for p in POSTINGS], LAST, TODAY, 7, prs=fake_prs)
assert res_pr["counts"]["Tailored"] == 3, res_pr["counts"]  # 1 tracker Gamma + PRs 10,11
assert res_pr["counts"]["Found"] == 1, res_pr["counts"]     # Theta (tracker) only; Iota+Kappa now Tailored
assert res_pr["awaiting_action"]["tailored_to_submit"] == 3

# gh unavailable (prs=None) -> tracker-only counts, old Found semantics
res_none = s.compute(APPS, POSTINGS, LAST, TODAY, 7, prs=None)
assert res_none["counts"]["Tailored"] == 1 and res_none["counts"]["Found"] == 3

# ---------------------------------------------------------------- boundaries
# no baseline -> nothing announced
assert s.compute(APPS, POSTINGS, None, TODAY, 7)["new"] == []
# baseline == today -> nothing announced, but today's ageing still reported
r_same = s.compute(APPS, POSTINGS, TODAY, TODAY, 7)
assert r_same["new"] == [] and len(r_same["ageing"]) == 1
# window catches backdated submissions across a missed run
assert [a["company"] for a in s.compute(APPS, POSTINGS, dt.date(2026, 10, 3), TODAY, 7)["new"]] \
    == ["Acme Corp"]

# ---------------------------------------------------------------- days flag
assert s.compute(APPS, POSTINGS, TODAY, TODAY, 20)["ageing"] == []

# short "awaiting action" line when nothing changed but items exist
r_quiet = s.compute(APPS, POSTINGS, TODAY, TODAY, 20)
assert r_quiet["changed"] is False and r_quiet["silent"] is False
quiet = s.render_text(r_quiet)
assert "no changes" in quiet and "awaiting action:" in quiet, quiet
assert "awaiting action: 1 tailored to submit, 3 found to tailor" in quiet, quiet

# ---------------------------------------------------------------- silence
r_empty = s.compute([], [], LAST, TODAY, 7)
assert r_empty["silent"] is True
assert s.render_text(r_empty) == ""

# ---------------------------------------------------------------- json
obj = json.loads(s.render_json(res))
assert obj["counts"]["Found"] == 3
assert obj["new"][0]["company"] == "Acme Corp"
assert obj["new"][0]["status"] == "Applied"
assert obj["ageing"][0]["age_days"] == 16
assert obj["silent"] is False
assert res["n_found"] == 2, res  # Iota + Kappa added after LAST

# ---------------------------------------------------------------- digest contract (template Message 1)
# Option A merges the two count lines; the day-over-day change rides on it.
assert text.splitlines()[2] == \
    "Applied 2 · Callbacks 0 · OA 0 · Interviews 1 · Offers 1 · " \
    "Rejections 2 · +1 applied, +2 found, 0 replies", text
assert 'no changes' not in text, text
assert '"""' not in text  # no stray quote marks


def _no_raw_urls(t):
    stripped = _re.sub(r'<a href="[^"]*">.*?</a>', '', t)
    assert "http" not in stripped, stripped


_no_raw_urls(text)

# ---------------------------------------------------------------- nudges (Message 2)
# Beta: Applied 2026-09-20 -> 16 days waiting -> chase now. Acme applied
# today (0 days) is not yet waiting and is excluded.
entries, silent = s.compute_nudges(APPS, TODAY)
assert silent is False
assert [(e["company"], e["days"], e["action"]) for e in entries] == \
    [("Beta Pte Ltd", 16, "chase now")], entries

# day-1 inclusion + 14-day flip boundary
edge = [{"company": "C1", "role": "R1",
         "link": "https://example.com/jobs/c1",
         "status": "Applied", "applied_date": "2026-10-05"},
        {"company": "C2", "role": "R2",
         "link": "https://example.com/jobs/c2",
         "status": "Applied", "applied_date": "2026-09-23"},
        {"company": "C3", "role": "R3",
         "link": "https://example.com/jobs/c3",
         "status": "Applied", "applied_date": "2026-09-22"},
        {"company": "C4", "role": "R4",
         "link": "https://example.com/jobs/c4",
         "status": "Interview", "applied_date": "2026-09-01"},
        {"company": "C5", "role": "R5",  # no link -> skipped silently
         "status": "Applied", "applied_date": "2026-09-01"},
        {"company": "C6", "role": "R6",  # no date -> skipped silently
         "link": "https://example.com/jobs/c6", "status": "Applied"}]
entries, _ = s.compute_nudges(edge, TODAY)
by_co = {e["company"]: e for e in entries}
assert set(by_co) == {"C1", "C2", "C3"}, by_co
assert (by_co["C1"]["days"], by_co["C1"]["action"]) == (1, "chase in 13 days")
assert (by_co["C2"]["days"], by_co["C2"]["action"]) == (13, "chase in 1 day")
assert (by_co["C3"]["days"], by_co["C3"]["action"]) == (14, "chase now")

# merge: duplicate company roles join with ' / ', earliest date wins
merge_apps = [{"company": "Dup Co", "role": "Backend Intern",
               "link": "https://example.com/jobs/d1",
               "status": "applied", "applied_date": "2026-10-01"},
              {"company": "Dup Co", "role": "Frontend Intern",
               "link": "https://example.com/jobs/d2",
               "status": "Applied", "applied_date": "2026-09-25"},
              {"company": "Dup Co", "role": "Backend Intern",
               "link": "https://example.com/jobs/d1",
               "status": "applied", "applied_date": "2026-10-03"}]
entries, _ = s.compute_nudges(merge_apps, TODAY)
assert len(entries) == 1, entries
assert entries[0]["roles"] == ["Backend Intern", "Frontend Intern"], entries
assert entries[0]["date"] == dt.date(2026, 9, 25), entries
assert entries[0]["link"] == "https://example.com/jobs/d2", entries

nudge_text = s.render_nudges(s.compute_nudges(APPS, TODAY)[0], TODAY)
assert nudge_text.splitlines()[0] == "jobs · nudges · 06/10/2026", nudge_text
assert nudge_text.splitlines()[1] == "Needs a nudge · 06/10/2026", nudge_text
assert "### Chase now · 1 · 14d or more" in nudge_text, nudge_text
assert ('1. **<a href="https://example.com/jobs/beta">Beta Pte Ltd</a>** · '
        'Software Engineer Intern · 16d') in nudge_text, nudge_text
assert "Software Engineer Intern" in nudge_text  # full role, never truncated
# the heading states the action, so the per-entry action words are gone
assert "chase now" not in nudge_text and "chase in" not in nudge_text, nudge_text
assert "applied " not in nudge_text, nudge_text
assert nudge_text.rstrip().endswith(s.NUDGE_TRAILER), nudge_text
assert nudge_text.count("\n") + 1 <= s.NUDGE_MAX_LINES
assert len(nudge_text) <= s.MESSAGE_CHAR_GUARD
assert not any(ch in nudge_text for ch in "\u2014\u2013"), nudge_text
assert '"""' not in nudge_text
_no_raw_urls(nudge_text)

# silent-when-empty: nothing Applied / all rows skipped
assert s.compute_nudges([], TODAY) == ([], True)
assert s.render_nudges([], TODAY) == ""
assert s.render_nudges(s.compute_nudges(
    [{"company": "X", "role": "R", "status": "Applied",
      "applied_date": "2026-10-06",  # 0 days -> not waiting yet
      "link": "https://example.com/jobs/x"}], TODAY)[0], TODAY) == ""

# ---------------------------------------------------------------- newpost (Message 3)
with open(os.path.join(FIX, "rankings.json"), encoding="utf-8") as fh:
    RANK = json.load(fh)
SKIPPED = s.load_skipped(os.path.join(FIX, "skipped.yaml"))
assert len(SKIPPED) == 2, SKIPPED

scores = {}
for r in RANK["ranked"]:
    scores[s.normalize_url(r["link"]).lower()] = r["score"]

NP_POSTS = list(POSTINGS) + [
    {"company": "Lambda Ltd", "role": "Data Intern",
     "link": "https://example.com/jobs/lambda", "added": "2026-10-04"},
    {"company": "Old Corp", "role": "Ancient Intern",
     "link": "https://example.com/jobs/old", "added": "2026-08-01"},
    {"company": "Mu Corp", "role": "QA Intern",
     "link": "https://example.com/jobs/mu", "added": "2026-10-05"},
    {"company": "Doom Inc", "role": "Ended Intern",
     "link": "https://example.com/jobs/doom", "added": "2026-10-05",
     "term": "May-Jun 2027", "closes": "30/09/2026"},
]
PR_FIX = [{"number": 11,
           "headRefName": "app/kappa-pte-ltd-security-intern",
           "title": "app(kappa-pte-ltd-security-intern): tailored variant",
           "body": "no link here"}]

eligible, skipped_list, ranked = s.compute_newpost(
    NP_POSTS, APPS, SKIPPED, TODAY, scores=scores, prs=PR_FIX)
assert ranked is True
# excluded: Acme (tracked), Kappa (open tailor PR #11), Old (>30d),
# Mu (skipped), Doom (term ended)
assert [e["posting"]["company"] for e in eligible] == \
    ["Iota Inc", "Lambda Ltd"], eligible
assert [(e["score"]) for e in eligible] == [87, 61]
reasons = {x["company"]: x["reason"] for x in skipped_list}
assert reasons["Acme Corp"] == "already tracked", reasons
assert reasons["Kappa Pte Ltd"] == "open tailor PR #11", reasons
assert reasons["Old Corp"] == "older than 30 days", reasons
assert reasons["Mu Corp"] == "skipped 05/10/2026 (fit)", reasons
assert reasons["Doom Inc"] == "term ended 30/09/2026", reasons

np_text = s.render_newpost(eligible, skipped_list, ranked, TODAY)
assert np_text.splitlines()[0] == "job-scout · newpost · 06/10/2026", np_text
assert np_text.splitlines()[1] == "New postings · 06/10/2026", np_text
# Option A: one-line plain tally, then a heading carrying the fit floor
assert np_text.splitlines()[2] == \
    "2 new · 0 strong · 1 good · 1 rest · sorted by fit", np_text
assert "### Worth applying · fit 61+" in np_text, np_text
# one full entry line, verbatim: number, bold company, role, score, age
assert ('1. **<a href="https://example.com/jobs/iota">Iota Inc</a>** · '
        'Cloud Intern · 87 · today') in np_text, np_text
# the score is a bare number on the row (only the heading says "fit")
assert "fit 87" not in np_text and "fit 61 " not in np_text, np_text
assert "posted" not in np_text, np_text
assert np_text.count("Skipped:") <= 2
assert np_text.rstrip().endswith(s.NEWPOST_TRAILER), np_text
assert np_text.count("\n") + 1 <= s.NEWPOST_MAX_LINES
assert len(np_text) <= s.MESSAGE_CHAR_GUARD
assert not any(ch in np_text for ch in "\u2014\u2013"), np_text
assert '"""' not in np_text
_no_raw_urls(np_text)

# top-5 cap + skipped cap: 7 eligible, 4 skipped
many = [{"company": f"C{i}", "role": "R Intern",
         "link": f"https://example.com/jobs/c{i}", "added": "2026-10-06"}
        for i in range(7)]
scores_many = {s.normalize_url(p["link"]).lower(): 90 - i
                for i, p in enumerate(many)}
mg, ms, _ = s.compute_newpost(many, [], [], TODAY,
                              scores=scores_many, prs=[])
big = s.render_newpost(mg, [{"company": f"S{i}", "role": "R",
                             "reason": "already tracked"} for i in range(4)],
                       True, TODAY)
# Option A: the truncation notice lives in the heading, not a tail line
assert "### Worth applying · fit 86+ · +2 not shown" in big, big
assert not any(ln.strip() == "+2 more" for ln in big.splitlines()), big
assert "+2 more skipped" in big, big
assert big.count("Skipped:") == 2

# posted-date fallback: no rankings -> sort newest first, says so
ug, us, ranked2 = s.compute_newpost(NP_POSTS, APPS, SKIPPED, TODAY,
                                    scores=None, prs=PR_FIX)
assert ranked2 is False
assert [e["posting"]["company"] for e in ug] == ["Iota Inc", "Lambda Ltd"]
plain = s.render_newpost(ug, us, ranked2, TODAY)
assert "sorted by posted date" in plain and "fit " not in plain, plain
_no_raw_urls(plain)

# char guard: a pathological run degrades by whole lines, never mid-tag
fat = [{"company": "C" * 60, "role": "R" * 400,
        "link": f"https://example.com/jobs/fat{i}", "added": "2026-10-06"}
       for i in range(7)]
fat_scores = {s.normalize_url(p["link"]).lower(): 99 - i
              for i, p in enumerate(fat)}
fg, fs, _ = s.compute_newpost(fat, [], [], TODAY, scores=fat_scores, prs=[])
fout = s.render_newpost(fg, fs, True, TODAY)
assert len(fout) <= s.MESSAGE_CHAR_GUARD, len(fout)
assert fout.rstrip().endswith(s.NEWPOST_TRAILER), fout[-200:]
assert fout.count("\n") + 1 <= s.NEWPOST_MAX_LINES, fout.count("\n")
_no_raw_urls(fout)

# silent-when-empty
assert s.render_newpost([], [], True, TODAY) == ""
eg2, _, _ = s.compute_newpost([], [], [], TODAY, scores={}, prs=[])
assert eg2 == []

# stale rankings file -> treated as unavailable
stale = os.path.join(tempfile.mkdtemp(), "rankings.json")
shutil.copy(os.path.join(FIX, "rankings.json"), stale)
old_ts = (dt.datetime.now() - dt.timedelta(hours=60)).timestamp()
os.utime(stale, (old_ts, old_ts))
assert s.load_rankings(stale)[0] is None
assert s.load_rankings(os.path.join(tempfile.mkdtemp(), "nope.json"))[0] is None
assert s.load_skipped(os.path.join(tempfile.mkdtemp(), "nope.yaml")) == []

# ---------------------------------------------------------------- state i/o
tmp = tempfile.mkdtemp()
try:
    missing = os.path.join(tmp, "nope.json")
    assert s.read_state(missing) is None
    state = os.path.join(tmp, "summary_state.json")
    s.write_state(state, TODAY, "2026-10-06T21:00:00+08:00")
    assert s.read_state(state) == TODAY
    with open(state, encoding="utf-8") as fh:
        assert json.load(fh)["last_digest"] == "2026-10-06"
    with open(os.path.join(tmp, "broken.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert s.read_state(os.path.join(tmp, "broken.json")) is None

    # ------------------------------------------------------------ main()
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "data"))
    shutil.copy(os.path.join(FIX, "applications.yaml"),
                os.path.join(repo, "applications.yaml"))
    shutil.copy(os.path.join(FIX, "postings.json"),
                os.path.join(repo, "data", "postings.json"))
    tracker_before = open(os.path.join(repo, "applications.yaml"), "rb").read()
    listing_before = sorted(os.listdir(repo))

    s.STATE_FILE = os.path.join(tmp, "main_state.json")
    s.fetch_open_tailor_prs = lambda log=None: None  # offline: no live gh in tests
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = s.main(["--repo", repo, "--dry-run"])
    assert rc == 0
    assert buf.getvalue().strip().splitlines()[0].startswith("jobs · summary · "), \
        buf.getvalue()
    assert buf.getvalue().splitlines()[1].startswith("Daily digest ·"), buf.getvalue()
    assert not os.path.exists(s.STATE_FILE), "--dry-run must not write state"
    # read-only guarantee: nothing in the repo changed
    assert open(os.path.join(repo, "applications.yaml"), "rb").read() == tracker_before
    assert sorted(os.listdir(repo)) == listing_before

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = s.main(["--repo", repo])
    assert rc == 0
    assert s.read_state(s.STATE_FILE) == s.sgt_today()

    # --json mode still emits a parseable object and is silent-safe
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s.main(["--repo", repo, "--json", "--dry-run"])
    assert json.loads(buf.getvalue())["counts"]["Found"] == 3

    # empty repo -> silent (no stdout) and still exit 0
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    s.STATE_FILE = os.path.join(tmp, "empty_state.json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = s.main(["--repo", empty])
    assert rc == 0 and buf.getvalue() == "", repr(buf.getvalue())

    # ------------------------------------------------- mode subcommands
    s.STATE_FILE = os.path.join(tmp, "mode_state.json")
    s.fetch_open_tailor_prs = lambda log=None: []  # offline
    s.TODAY = TODAY
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = s.main(["nudges", "--repo", repo, "--dry-run"])
        assert rc == 0, rc
        nout = buf.getvalue()
        assert nout.splitlines()[0].startswith("jobs · nudges · "), repr(nout)
        assert nout.splitlines()[1] == "Needs a nudge · 06/10/2026", repr(nout)
        assert s.NUDGE_TRAILER in nout
        _no_raw_urls(nout)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = s.main(["--mode", "newpost", "--repo", repo,
                         "--skipped", os.path.join(FIX, "skipped.yaml"),
                         "--rankings", os.path.join(FIX, "rankings.json"),
                         "--dry-run"])
        assert rc == 0, rc
        pout = buf.getvalue()
        # rankings fixture is stale-dated in real time, so the message
        # degrades gracefully to posted-date order
        assert pout == "" or pout.splitlines()[1].startswith("New postings · "), \
            repr(pout)
        if pout:
            assert s.NEWPOST_TRAILER in pout
            _no_raw_urls(pout)

        # nudges/newpost never write state, even without --dry-run
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = s.main(["nudges", "--repo", repo])
        assert rc == 0 and not os.path.exists(s.STATE_FILE)
    finally:
        s.TODAY = None
finally:
    shutil.rmtree(tmp)

print("test_summary: all assertions passed")
