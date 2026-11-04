#!/usr/bin/env python3
"""Offline self-check for discover.py: filters, parsing, dedup merging.

No network. Run: python3 test_discover.py
"""
from datetime import date

import discover as d

TODAY = date(2026, 10, 6)

# ---------------------------------------------------------------- role filter
good = [
    "Software Engineer Intern",
    "Software Engineering Intern",
    "SWE Intern",
    "Software Developer Intern",
    "Backend Intern",
    "Back-end Engineering Intern",
    "Frontend Developer Intern",
    "Full-Stack Engineer Intern",
    "Mobile App Developer Intern",
    "Android Developer Intern",
    "iOS Engineer Intern",
    "Data Scientist Intern",
    "Data Engineer Intern",
    "Data Analyst Intern",
    "Machine Learning Engineer Intern",
    "ML Intern",
    "AI Engineer Intern",
    "Computer Vision Intern",
    "DevOps Intern",
    "Cloud Engineer Intern",
    "Platform Engineer Intern",
    "Security Engineering Intern",
    "QA Automation Intern",
    "Programmer Intern",
]
for r in good:
    assert d.is_intern_role(r), f"should match: {r}"

bad = [
    # non-engineering tokens exclude, even next to a tech word
    "Marketing Intern",
    "Content Writer Intern",
    "Events Intern",
    "HR Admin Intern",
    "Finance Analyst Intern",
    "Web, Customer Relation Intern",
    "Group IT PMO Intern",
    "Group IT Helpdesk Intern",
    "Project Management Officer (PMO) / Quality Assurance (QA) Intern",
    "Tech Product Management Intern",
    "Intern, IT",
    "Group IT / Enterprise Applications Intern",
    "Group IT Security Intern",
    "Security Operations Intern - IT Division",
    "Marketing & Growth Intern \u2013 AI & Robotics",
    "Business Analyst Intern",
    "Growth Intern",
    "Sales Intern",
    "Business Development Intern",
    "IT Support Intern",
    "Operations Intern",
    "Digital IT Intern",
    "Technology Intern",
    # seniority / experience
    "Senior Software Engineer Intern",
    "Lead Data Intern",
    "Engineering Manager Intern",
    "Software Engineer (5+ years)",
    "Software Engineer Intern (5+ years)",
    "Principal Developer Intern",
]
for r in bad:
    assert not d.is_intern_role(r), f"should reject: {r}"

# ---------------------------------------------------------------- location
assert d.location_ok("Bukit Merah, SG")
assert d.location_ok("Singapore")
assert d.location_ok("Remote (Singapore)")
assert not d.location_ok("Kuala Lumpur, MY")
assert not d.location_ok("")
assert d.location_ok("", assume_sg=True)

# ---------------------------------------------------------------- dates
assert d.parse_posted("05 Oct", TODAY) == date(2026, 10, 5)
assert d.parse_posted("05 Oct 2026", TODAY) == date(2026, 10, 5)
assert d.parse_posted("12 Jan", TODAY) == date(2026, 1, 12)
# future-looking month without year rolls back a year (Dec seen in Jan)
assert d.parse_posted("31 Dec", date(2026, 1, 4)) == date(2025, 12, 31)
assert d.parse_posted("nonsense", TODAY) is None
assert d.posted_ok(None, TODAY)
assert d.posted_ok(date(2026, 9, 10), TODAY)      # 26 days
assert not d.posted_ok(date(2026, 8, 1), TODAY)    # 66 days
assert not d.posted_ok(date(2027, 1, 1), TODAY)    # far future

# ---------------------------------------------------------------- dedup key
a = {"company": "Acme  Corp", "role": "Software Engineer Intern",
     "link": "https://acme.com/jobs/1?ref=x"}
b = {"company": "acme corp", "role": "software engineer intern",
     "link": "https://acme.com/jobs/1/"}
c = {"company": "Acme Corp", "role": "Software Engineer Intern",
     "link": "https://acme.com/jobs/2"}
assert d.dedup_key(a) == d.dedup_key(b), (d.dedup_key(a), d.dedup_key(b))
assert d.dedup_key(a) != d.dedup_key(c)

# ---------------------------------------------------------------- HTML parse
LISTING_HTML = """
<a class="ast-row job-listing-row list-odd" href="https://www.internsg.com/job/acme-software-engineer-intern-1/">
<div class="ast-col-lg-5 job-listing-main"><span class="job-listing-title">Software Engineer Intern</span>
<div class="job-listing-company">Acme Corp<span class="text-monospace badge badge-warning">acme.com</span></div></div>
<div class="ast-col-lg-2"><span class="job-listing-dt">Bukit Merah, SG</span></div>
<div class="ast-col-lg-4"><div class="job-listing-dt">From Nov 2026, For At Least 6 Months</div>
<span class="text-monospace badge badge-info">Intern/TS</span></div>
<div class="ast-col-lg-1"><span class="text-monospace badge badge-success" data-tooltip="Click for Details"> 05 Oct </span></div>
</a>
<a class="ast-row job-listing-row list-even" href="https://www.internsg.com/job/beta-marketing-intern/">
<div class="ast-col-lg-5 job-listing-main"><span class="job-listing-title">Marketing Intern</span>
<div class="job-listing-company">Beta Pte Ltd</div></div>
<div class="ast-col-lg-2"><span class="job-listing-dt">Geylang, SG</span></div>
<div class="ast-col-lg-4"><div class="job-listing-dt">Flexible Start</div></div>
<div class="ast-col-lg-1"><span class="text-monospace badge badge-success"> 30 Sep </span></div>
</a>
<a class="ast-row job-listing-row list-odd" href="https://www.internsg.com/job/gamma-senior-dev-intern/">
<div class="ast-col-lg-5 job-listing-main"><span class="job-listing-title">Senior Data Intern</span>
<div class="job-listing-company">Gamma</div></div>
<div class="ast-col-lg-2"><span class="job-listing-dt">Singapore</span></div>
<div class="ast-col-lg-4"><div class="job-listing-dt">Flexible Start</div></div>
<div class="ast-col-lg-1"><span class="text-monospace badge badge-success"> 01 Oct </span></div>
</a>
"""
HOME_HTML = """
<div class="ast-row latestJobItem"><div class="ast-col-sm-2">
<span class="text-monospace badge badge-secondary">05 Oct</span></div>
<div class="ast-col-sm-10">Delta Systems Pte Ltd &#8211; <a href="https://www.internsg.com/job/delta-backend-engineer-intern/">Backend Engineer Intern</a></div></div>
<div class="ast-row latestJobItem"><div class="ast-col-sm-2">
<span class="text-monospace badge badge-secondary">04 Oct</span></div>
<div class="ast-col-sm-10">Epsilon Media &#8211; <a href="https://www.internsg.com/job/epsilon-media-intern/">Media Intern</a></div></div>
"""
parsed = d.parse_internsg(LISTING_HTML) + d.parse_internsg(HOME_HTML)
assert len(parsed) == 5, len(parsed)
assert parsed[0]["company"] == "Acme Corp"
assert parsed[0]["role"] == "Software Engineer Intern"
assert parsed[0]["location"] == "Bukit Merah, SG"
assert parsed[0]["posted_raw"] == "05 Oct"
assert parsed[0]["link"] == "https://www.internsg.com/job/acme-software-engineer-intern-1"
assert parsed[3]["company"] == "Delta Systems Pte Ltd", parsed[3]["company"]
assert parsed[3]["role"] == "Backend Engineer Intern"

# filters applied to the parsed rows (source InternSG -> location may be empty)
for p in parsed:
    p["source"] = "InternSG"
norm = [d.normalize(p, today=TODAY) for p in parsed]
kept = d.filter_postings(norm, TODAY, days=30)
roles = sorted(p["role"] for p in kept)
assert roles == ["Backend Engineer Intern", "Software Engineer Intern"], roles

# ---------------------------------------------------------------- companies.yaml
CFG = """
# comment
- name: Acme Corp
  url: https://acme.example/careers
  location: Singapore
  link_contains: [/jobs/]
  role_contains: [intern]
- name: Beta Pte Ltd
  url: https://beta.example/careers
  remote: false
"""
cfgs = d.parse_companies(CFG)
assert len(cfgs) == 2, cfgs
assert cfgs[0]["name"] == "Acme Corp"
assert cfgs[0]["link_contains"] == ["/jobs/"]
assert cfgs[1]["remote"] is False
assert d.parse_companies("# only comments\n[]\n") == []

CAREER_HTML = """
<a href="/jobs/1">Software Engineering Intern</a>
<a href="/jobs/2">Sales Manager</a>
<a href="/about">About us</a>
<a href="https://acme.example/jobs/3">Data Engineer Intern (Remote)</a>
"""
cfg = {"name": "Acme Corp", "url": "https://acme.example/careers",
       "location": "Singapore", "link_contains": ["/jobs/"]}
cp = d.parse_company_page(CAREER_HTML, cfg)
assert len(cp) == 2, cp
assert cp[0]["link"] == "https://acme.example/jobs/1"
assert cp[1]["link"] == "https://acme.example/jobs/3"

# ---------------------------------------------------------------- merging
existing = [{
    "company": "Acme Corp", "role": "Software Engineer Intern",
    "link": "https://acme.example/jobs/1", "source": "InternSG",
    "location": "Singapore", "posted": "2026-10-05", "key": None, "added": "2026-10-06",
}]
existing[0]["key"] = d.dedup_key(existing[0])
incoming = [
    # exact duplicate (different spelling/url cosmetics) -> skipped
    {"company": "acme  corp", "role": "software engineer intern",
     "link": "https://acme.example/jobs/1/?ref=1", "source": "InternSG"},
    # new posting
    {"company": "Delta Systems Pte Ltd", "role": "Backend Engineer Intern",
     "link": "https://acme.example/jobs/2", "source": "InternSG"},
]
merged, new = d.merge_postings(existing, incoming, TODAY)
assert len(merged) == 2 and len(new) == 1, (len(merged), len(new))
assert merged[0]["added"] == "2026-10-06"          # existing untouched
assert merged[0]["company"] == "Acme Corp"        # not rewritten
assert new[0]["company"] == "Delta Systems Pte Ltd"
assert new[0]["key"] == d.dedup_key(new[0])

# merging again is a no-op (idempotency at the data layer)
merged2, new2 = d.merge_postings(merged, incoming, TODAY)
assert len(merged2) == 2 and new2 == []

# within-run coalesce keeps the richest duplicate
co = d.coalesce([
    {"company": "Same Co", "role": "Data Intern", "link": "https://x/j/1",
     "location": "", "posted": None},
    {"company": "Same Co", "role": "Data Intern", "link": "https://x/j/1/",
     "location": "Singapore", "posted": "2026-10-01"},
])
assert len(co) == 1 and co[0]["location"] == "Singapore" and co[0]["posted"] == "2026-10-01", co

# ---------------------------------------------------------------- import hook
imported = d.import_postings([
    {"company": "Future Co", "role": "SWE Intern",
     "link": "https://linkedin.com/jobs/view/9", "posted": "2026-09-30",
     "location": "Singapore"},
    {"company": "Nowhere Co", "role": "Software Intern",
     "link": "https://linkedin.com/jobs/view/10", "location": "Berlin, DE"},
], TODAY)
assert imported[0]["posted"] == "2026-09-30"
assert imported[1]["location"] == "Berlin, DE"
imp_kept = d.filter_postings(imported, TODAY, days=30, assume_sg=True)
assert [p["role"] for p in imp_kept] == ["SWE Intern"], imp_kept

# the import path uses the identical SWE-only role filter
imp2 = d.import_postings([
    {"company": "Li Co", "role": "Backend Engineer Intern",
     "link": "https://linkedin.com/jobs/view/20", "location": "Singapore",
     "posted": "2026-10-01"},
    {"company": "Ad Co", "role": "Marketing Intern",
     "link": "https://linkedin.com/jobs/view/21", "location": "Singapore",
     "posted": "2026-10-01"},
    {"company": "IT Co", "role": "Group IT Helpdesk Intern",
     "link": "https://linkedin.com/jobs/view/22", "location": "Singapore",
     "posted": "2026-10-01"},
], TODAY)
role_kept, role_rejected = d.role_filter_split(imp2)
assert [p["role"] for p in role_kept] == ["Backend Engineer Intern"], role_kept
assert sorted(p["role"] for p in role_rejected) == [
    "Group IT Helpdesk Intern", "Marketing Intern"], role_rejected
assert len(role_rejected) == 2

# --------------------------------------- generic-link drop at import
assert d.is_generic_link("https://www.linkedin.com/jobs/search")
assert d.is_generic_link("https://www.linkedin.com/jobs/browse")
assert not d.is_generic_link("https://www.linkedin.com/jobs/view/123")
imp3 = d.import_postings([
    {"company": "Thales", "role": "Software Engineer Intern (Python)",
     "link": "https://www.linkedin.com/jobs/search", "location": "Singapore"},
    {"company": "Ok Co", "role": "Software Engineer Intern",
     "link": "https://www.linkedin.com/jobs/view/30", "location": "Singapore"},
], TODAY)
assert [p["company"] for p in imp3] == ["Ok Co"], imp3

# --- merge_postings: company+role secondary dedupe ---
_TODAY = date(2026, 10, 9)
existing = [{
    "company": "Airwallex", "role": "Software Engineer Intern (Summer 2027)",
    "link": "https://www.linkedin.com/jobs/view/4445083875", "source": "LinkedIn",
}]
incoming = [
    # same company+role, different URL (repost) -> skipped
    {"company": "Airwallex", "role": "Software Engineer Intern (Summer 2027)",
     "link": "https://www.linkedin.com/jobs/view/9999999", "source": "LinkedIn"},
    # genuinely new posting -> kept
    {"company": "Ok Co", "role": "Platform Intern",
     "link": "https://www.linkedin.com/jobs/view/123", "source": "LinkedIn"},
]
merged, new = d.merge_postings(existing, incoming, _TODAY)
assert [p["company"] for p in new] == ["Ok Co"], new
assert len(merged) == 2, merged

# same company+role twins inside the store itself are collapsed on next merge
existing_twins = existing + [{
    "company": "Airwallex", "role": "Software Engineer Intern (Summer 2027)",
    "link": "https://www.linkedin.com/jobs/search", "source": "LinkedIn",
}]
merged2, new2 = d.merge_postings(existing_twins, [], _TODAY)
assert len(merged2) == 2, merged2
assert new2 == []

# ---------------------------------------------------------------- board rows
# `--boards` folds probe_boards.py rows into the same fetch path, so board
# postings must survive exactly the same normalize -> role -> location -> dedupe
# chain as InternSG rows. These assertions pin that contract offline (the
# adapter itself is exercised by `probe_boards.py --list`; no network here).
board_rows = [
    {"company": "Acme Corp", "role": "Software Engineer Intern",
     "link": "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/SG/1",
     "source": "Workday", "location": "Singapore, SG", "posted": "2026-10-05"},
    {"company": "Acme Corp", "role": "Account Executive, Business Intern",
     "link": "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/SG/2",
     "source": "Workday", "location": "Singapore, SG"},
    {"company": "Beta Ltd", "role": "Data Engineer Intern",
     "link": "https://boards-api.greenhouse.io/beta/3",
     "source": "Greenhouse", "location": "Kuala Lumpur, MY"},
]
board_norm = [d.normalize(p, today=TODAY) for p in board_rows]
# ISO dates from an adapter survive normalize untouched (not re-parsed as text)
assert board_norm[0]["posted"] == "2026-10-05", board_norm[0]
board_kept, board_rejected = d.role_filter_split(board_norm)
assert [p["role"] for p in board_kept] == ["Software Engineer Intern", "Data Engineer Intern"]
assert [p["role"] for p in board_rejected] == ["Account Executive, Business Intern"]
board_ok = d.filter_postings(board_kept, TODAY, days=30)
assert [p["company"] for p in board_ok] == ["Acme Corp"], board_ok  # MY dropped
# a board row that duplicates an InternSG row collapses to one entry
board_dup, _ = d.merge_postings([{
    "company": "Acme Corp", "role": "Software Engineer Intern",
    "link": "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/SG/1",
    "source": "InternSG", "location": "Singapore", "posted": "2026-10-05",
}], board_ok, TODAY)
assert len(board_dup) == 1, board_dup

# --- collect_boards: one failing board must not cost the others ---
import sys
import types
fake = types.ModuleType("probe_boards")
fake.BOARDS = [{"name": "Good", "ats": "x"}, {"name": "Broken", "ats": "y"},
               {"name": "Quiet", "ats": "z"}]
fake.probe = lambda cfg: {
    "Good": ("PROBED", [board_rows[0]], "1 rows, 1 Singapore"),
    "Broken": ("BAD-TENANT", [], "HTTP 422 from api"),
    "Quiet": ("PROBED 0", [], "40 rows, 0 Singapore"),
}[cfg["name"]]
_saved = sys.modules.get("probe_boards")
sys.modules["probe_boards"] = fake
lines = []
try:
    got, errs = d.collect_boards(TODAY, log=lines.append)
finally:
    if _saved is None:
        del sys.modules["probe_boards"]
    else:
        sys.modules["probe_boards"] = _saved
assert [p["company"] for p in got] == ["Acme Corp"], got
assert len(errs) == 1 and "BAD-TENANT" in errs[0], errs
assert any("board-error: Broken" in line for line in lines), lines
assert any("board-empty: Quiet" in line for line in lines), lines

# a board whose probe() raises (incompatible module) is one line, not a crash
fake.probe = lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")) if cfg["name"] == "Broken" else ("PROBED", [board_rows[0]], "1 rows")
_saved = sys.modules.get("probe_boards")
sys.modules["probe_boards"] = fake
lines = []
try:
    got3, errs3 = d.collect_boards(TODAY, log=lines.append)
finally:
    if _saved is None:
        del sys.modules["probe_boards"]
    else:
        sys.modules["probe_boards"] = _saved
assert [p["company"] for p in got3] == ["Acme Corp"] * 2, got3
assert len(errs3) == 1 and "RuntimeError: boom" in errs3[0], errs3

# a missing probe_boards module degrades to one logged line, never a raise
sys.modules["probe_boards"] = None
try:
    got2, errs2 = d.collect_boards(TODAY, log=lambda *a: None)
finally:
    if _saved is None:
        del sys.modules["probe_boards"]
    else:
        sys.modules["probe_boards"] = _saved
assert got2 == [] and len(errs2) == 1 and "unavailable" in errs2[0], errs2


print("test_discover: all assertions passed")

# --- --boards + --import-json is refused, not silently ignored -------------
import io as _io, contextlib as _ctx
_buf_out, _buf_err = _io.StringIO(), _io.StringIO()
with _ctx.redirect_stdout(_buf_out), _ctx.redirect_stderr(_buf_err):
    _rc = d.main(["--boards", "--import-json", "/dev/null", "--dry-run"])
assert _rc == 2, f"--boards with --import-json must exit 2, got {_rc}"
assert "cannot be combined" in _buf_err.getvalue(), (
    f"expected a refusal on stderr, got {_buf_err.getvalue()!r}")
print("  ok: --boards + --import-json refused with rc=2")

# the flag is advertised in the parser
assert "--boards" in d.build_parser().format_help(), (
    "--boards must be documented in the parser help")
print("  ok: --boards present in parser help")
