#!/usr/bin/env python3
"""Offline self-check for stages.py: line-oriented tracker edits and mail matching.

No network, no writes outside a temp dir. Run: python3 test_stages.py
"""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile

import stages as st
import summary as s

HERE = os.path.dirname(os.path.abspath(__file__))

# A miniature tracker with the real file's shape: comments, a folded multi-line
# note, key order, a row that skips Callback/OA, and a Rejected row.
TRACKER = """\
# Applications tracker (hand-maintained)
# one row per application, newest last
- company: Lumen AI
  role: Founding AI Engineer
  link: https://www.internsg.com/job/lumen
  source: InternSG
  status: applied
  applied_date: 2026-10-06
  pr: https://github.com/demo-user/resume/pull/9
  notes: Workday ATS (req JR005033); availability Jan-Jun 2027; work authorisation
    Student Pass Holder; commitment Full Time; prior interview No.
# a comment between rows belongs to neither row's keys
- company: Northwind Bank
  role: Backend Developer
  status: rejected
  rejected_date: 2026-10-19
  applied_date: 2026-10-07
- company: Cobalt Labs
  role: Software Engineer Intern
  status: applied
  applied_date: 2026-10-12
  notes: submitted via ATS
"""

TODAY = dt.date(2026, 10, 31)


def _run(argv):
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = st.main(argv)
    return rc, buf.getvalue() + err.getvalue()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- record split
tmp = tempfile.mkdtemp()
try:
    path = os.path.join(tmp, "applications.yaml")
    _write(path, TRACKER)

    lines, apps = st.read_apps(path)
    assert len(apps) == 3, [a["row"].get("company") for a in apps]
    assert [a["row"]["company"] for a in apps] == \
        ["Lumen AI", "Northwind Bank", "Cobalt Labs"]
    # folded notes are read the way summary.parse_yaml_list reads them
    assert apps[0]["row"]["notes"].endswith("prior interview No."), \
        apps[0]["row"]["notes"]
    assert "Student Pass Holder" in apps[0]["row"]["notes"]
    # summary's minimal parser drops folded continuation lines (it skips any
    # line without a colon); stages.py joins them, which is what the file means
    assert s.parse_yaml_list(TRACKER)[0]["notes"] == \
        "Workday ATS (req JR005033); availability Jan-Jun 2027; work authorisation"
    assert apps[0]["row"]["company"] == s.parse_yaml_list(TRACKER)[0]["company"]
    # the comment line is inside no field
    assert all("#" not in k for a in apps for k in a["row"])

    # ------------------------------------------------------------ list
    rc, out = _run(["list", "--tracker", path, "--today", "2026-10-31"])
    assert rc == 0
    list_rows = out.strip().splitlines()
    assert len(list_rows) == 3
    assert list_rows[0].startswith("Applied"), list_rows[0]
    assert "overdue" in list_rows[0], list_rows[0]
    assert list_rows[1].startswith("Rejected"), list_rows[1]
    assert "overdue" not in list_rows[1], list_rows[1]

    items = st.list_rows(apps, TODAY)
    by_company = {i["company"]: i for i in items}
    assert by_company["Lumen AI"]["days"] == 25
    assert by_company["Lumen AI"]["stage"] == "Applied"
    assert by_company["Lumen AI"]["stale"] is True
    assert by_company["Northwind Bank"]["stage"] == "Rejected"
    assert by_company["Northwind Bank"]["due_in"] is None
    assert by_company["Northwind Bank"]["stale"] is False

    rc, out = _run(["list", "--tracker", path, "--today", "2026-10-31",
                    "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert [p["company"] for p in payload] == \
        ["Lumen AI", "Northwind Bank", "Cobalt Labs"]
    assert payload[0]["stage_source"] == ""

    # ------------------------------------------------------------ set: forward
    before_other = _read(path)
    rc, out = _run(["set", "--tracker", path, "--company", "Lumen AI",
                    "--stage", "Callback", "--date", "2026-10-31",
                    "--source", "email",
                    "--evidence", "Recruiter asked for transcript"])
    assert rc == 0, out
    after = _read(path)
    assert after != before_other
    lines2, apps2 = st.read_apps(path)
    row = apps2[0]["row"]
    assert row["stage"] == "Callback"
    assert row["stage_since"] == "2026-10-31"
    assert row["stage_source"] == "email"
    assert row["stage_evidence"] == \
        "2026-10-31 email: Recruiter asked for transcript"
    # every pre-existing key and its value survive
    old = apps[0]["row"]
    for key, value in old.items():
        assert row[key] == value, (key, row[key], value)
    # folded notes still folded and intact
    assert ("    Student Pass Holder; commitment Full Time; prior interview No."
            in after.splitlines())
    # comments still present, in order
    assert after.splitlines()[0] == "# Applications tracker (hand-maintained)"
    assert "# a comment between rows belongs to neither row's keys" in after
    # other rows untouched
    assert apps2[1]["row"] == apps[1]["row"]
    assert apps2[2]["row"] == apps[2]["row"]
    # key order: stage keys appended after the row's own keys
    keys = list(row.keys())
    assert keys[:5] == ["company", "role", "link", "source", "status"], keys
    assert keys[-4:] == ["stage", "stage_since", "stage_source",
                         "stage_evidence"], keys

    # ``stage`` now overrides ``status`` for the rest of the system
    assert s.row_stage(row) == "Callback"
    assert row["status"] == "applied"

    # ------------------------------------------------------------ evidence appends
    rc, _ = _run(["set", "--tracker", path, "--company", "Lumen AI",
                  "--stage", "OA", "--date", "2026-11-02",
                  "--source", "email", "--evidence", "HackerRank invite"])
    assert rc == 0
    _, apps3 = st.read_apps(path)
    row3 = apps3[0]["row"]
    assert row3["stage"] == "OA"
    assert row3["stage_since"] == "2026-11-02"
    assert row3["stage_evidence"].count(" | ") == 1, row3["stage_evidence"]
    assert row3["stage_evidence"].startswith("2026-10-31 email:")
    assert row3["stage_evidence"].endswith("2026-11-02 email: HackerRank invite")
    assert len(row3.keys()) == len(old) + 4, list(row3.keys())
    assert "+stage: OA" not in _read(path)

    # ------------------------------------------------------------ backwards refused
    snapshot = _read(path)
    rc, out = _run(["set", "--tracker", path, "--company", "Lumen AI",
                    "--stage", "Applied", "--date", "2026-11-03"])
    assert rc == 3, (rc, out)
    assert "backwards transition OA -> Applied" in out, out
    assert _read(path) == snapshot

    # forced backwards move is allowed
    rc, out = _run(["set", "--tracker", path, "--company", "Lumen AI",
                    "--stage", "Applied", "--date", "2026-11-03", "--force"])
    assert rc == 0, out
    _, apps4 = st.read_apps(path)
    assert apps4[0]["row"]["stage"] == "Applied"

    # ------------------------------------------------------------ Rejected guard
    snapshot = _read(path)
    rc, out = _run(["set", "--tracker", path, "--company", "Northwind Bank",
                    "--stage", "Interview", "--date", "2026-11-01"])
    assert rc == 3, (rc, out)
    assert "already Rejected" in out, out
    assert _read(path) == snapshot
    rc, out = _run(["set", "--tracker", path, "--company", "Northwind Bank",
                    "--stage", "Interview", "--date", "2026-11-01", "--force"])
    assert rc == 0, out
    _, apps5 = st.read_apps(path)
    assert apps5[1]["row"]["stage"] == "Interview"

    # ------------------------------------------------------------ dry run writes nothing
    snapshot = _read(path)
    rc, out = _run(["set", "--tracker", path, "--company", "Cobalt Labs",
                    "--stage", "Interview", "--date", "2026-11-04",
                    "--dry-run"])
    assert rc == 0 and "would set" in out, out
    assert _read(path) == snapshot

    # ------------------------------------------------------------ ambiguous identity
    rc, out = _run(["set", "--tracker", path, "--company", "Nonexistent Co",
                    "--stage", "Applied", "--date", "2026-11-04"])
    assert rc == 1 and "no row for company" in out
    rc, out = _run(["set", "--tracker", path, "--company", "Lumen AI",
                    "--stage", "Nonsense", "--date", "2026-11-04"])
    assert rc == 2 and "unknown stage" in out

    # ------------------------------------------------------------ apply-email
    # a row that skips Callback and OA straight to Interview is legal
    mail = os.path.join(tmp, "mail.json")
    _write(mail, json.dumps({"new": [
        {"category": "interview", "from": "no-reply@greenhouse.io",
         "subject": "Interview - Cobalt Labs Software Engineer Intern"},
        {"category": "oa", "from": "careers@lumen.example",
         "subject": "Lumen AI online assessment"},
        {"category": "rejection", "from": "talent@vantage.example",
         "subject": "Update on your application to Vantage Analytics"},
        {"category": "follow_up", "from": "hr@unknownco.example",
         "subject": "Following up on your application"},
        {"category": "follow_up", "from": "recruiter@lumen.example",
         "subject": "Please provide your transcript and availability"},
        {"category": "unclassified_job", "from": "alerts@linkedin.com",
         "subject": "New jobs for you"},
    ], "unmatched": []}))

    snapshot = _read(path)
    rc, out = _run(["apply-email", "--tracker", path, "--json", mail,
                    "--today", "2026-10-31"])
    assert rc == 0, out
    assert "dry run: nothing written" in out, out
    assert _read(path) == snapshot
    assert "Cobalt Labs" in out and "-> Interview" in out, out
    assert "not guessed" in out
    # the generic follow-up and the job alert are never guessed
    assert "Following up on your application" in out
    assert "New jobs for you" in out

    rc, out = _run(["apply-email", "--tracker", path, "--json", mail,
                    "--today", "2026-10-31", "--commit", "--json-out"])
    assert rc == 0, out
    report = json.loads(out)
    assert report["committed"] is True
    assert report["written"] >= 1
    proposed = {(p["company"], p["to"]) for p in report["proposed"]}
    assert ("Cobalt Labs", "Interview") in proposed, proposed
    assert not any(p["company"] == "Vantage Analytics" for p in report["proposed"])
    assert any(u["from"] == "talent@vantage.example" for u in report["unmatched"])
    assert any(a["from"] == "hr@unknownco.example" for a in report["ambiguous"])

    _, apps6 = st.read_apps(path)
    codex = [a["row"] for a in apps6 if a["row"]["company"] == "Cobalt Labs"][0]
    assert codex["stage"] == "Interview", codex
    assert codex["stage_source"] == "email"
    assert codex["stage_evidence"].startswith("2026-10-31 email: Interview -")
    # skipped stages are not reported as missing steps anywhere
    _, out2 = _run(["list", "--tracker", path, "--today", "2026-10-31"])
    assert "missing" not in out2.lower(), out2

    # second pass is a no-op (idempotent)
    count_before = _read(path)
    rc, out = _run(["apply-email", "--tracker", path, "--json", mail,
                    "--today", "2026-10-31", "--commit"])
    assert rc == 0
    assert _read(path) == count_before

    # ------------------------------------------------------------ matcher units
    assert st.sender_domain("Recruiter <a.b@Meridian.sg>") == "meridian.sg"
    assert st.sender_domain("nobody") == ""
    assert st.company_tokens("Coherent Corp. Pte Ltd") == ["coherent"]
    assert st.company_tokens("Lumen AI") == ["lumen", "ai"]
    assert st.merge_evidence("", "2026-10-31 email: x") == "2026-10-31 email: x"
    long_ev = st.merge_evidence("2026-01-01 email: a" * 20, "2026-10-31 email: z")
    assert len(long_ev) <= st.EVIDENCE_LIMIT, long_ev
    assert long_ev.endswith("2026-10-31 email: z")
    assert st.transition_errors("Applied", "Interview") == []
    assert st.transition_errors("Applied", "Callback") == []
    assert st.transition_errors("Callback", "Applied") != []
    assert st.transition_errors("Callback", "Applied", force=True) == []
    assert st.transition_errors("Rejected", "Interview") != []
    assert st.transition_errors("Rejected", "Rejected") == []
    # a stage with no policy entry (Rejected) is never stale in list output
    assert s.NUDGE_POLICY["Rejected"] is None
    # the per-stage nudge thresholds this lane's list output measures against
    assert s.NUDGE_POLICY == {"Callback": 2, "OA": 5, "Interview": 3,
                             "Applied": 14, "Accepted": 0, "Tailored": 3,
                             "Found": 7, "Rejected": None}, s.NUDGE_POLICY
    assert st.list_rows(apps, TODAY)[1]["stale"] is False  # Rejected row
    assert s.stage_index("Interview") > s.stage_index("OA") > s.stage_index("Applied")
    # optional-stage jumps: Callback/OA are declared optional
    assert s.OPTIONAL_STAGES == ("Callback", "OA")
finally:
    import shutil
    shutil.rmtree(tmp)

print("test_stages: all assertions passed")
