#!/usr/bin/env python3
"""Synthetic CLI checks for repeatable records and audited corrections."""
import contextlib
import io
import json
import os
import tempfile

import outcomes
import recruitment_evidence as evidence


tmp = tempfile.mkdtemp(prefix="jobsearch-outcomes-")
try:
    tracker = os.path.join(tmp, "applications.yaml")
    ledger = os.path.join(tmp, "runtime", "recruitment_events.json")
    with open(tracker, "w", encoding="utf-8") as stream:
        stream.write("""- company: Atlas
  role: Data Intern
  link: https://jobs.example/role?job_id=42
  applied_date: 2026-01-10
  pr: https://github.com/demo/resume/pull/7
  status: applied
""")

    common = ["--ledger", ledger, "--tracker", tracker]
    record_args = common + ["record", "--pr", "7", "--type", "recruiter_reply",
                            "--date", "2026-02-01", "--origin", "human", "--id", "action-1",
                            "--evidence", "Recruiter asked for availability"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert outcomes.main(record_args) == 0
    first = json.loads(out.getvalue())
    assert first["event_type"] == "recruiter_reply" and not first["conflicted"]
    doc = json.load(open(ledger, encoding="utf-8"))
    assert len(doc["events"]) == 1
    first_revision = evidence._revision_id(doc["events"][0])

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert outcomes.main(record_args) == 0
    repeated = json.load(open(ledger, encoding="utf-8"))
    assert len(repeated["events"]) == 1
    assert evidence._revision_id(repeated["events"][0]) == first_revision

    conflict_args = common + ["record", "--pr", "7", "--type", "oa_invitation",
                              "--date", "2026-02-01", "--origin", "automated",
                              "--id", "action-1", "--evidence", "Assessment invitation"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert outcomes.main(conflict_args) == 0
    assert json.loads(out.getvalue())["conflicted"] is True
    conflicted = json.load(open(ledger, encoding="utf-8"))
    assert len(conflicted["events"]) == 2
    _, quarantined, _ = evidence.effective_events(conflicted)
    assert "manual:action-1" in quarantined

    correction_args = common + ["adjudicate", "--event-id", "manual:action-1",
                                "--pr", "7", "--type", "oa_completed",
                                "--date", "2026-02-02", "--origin", "automated",
                                "--evidence", "Reviewed the synthetic assessment receipt",
                                "--resolve-conflict"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert outcomes.main(correction_args) == 0
    assert json.loads(out.getvalue())["conflicted"] is False
    corrected = json.load(open(ledger, encoding="utf-8"))
    effective, quarantined, _ = evidence.effective_events(corrected)
    assert not quarantined
    chosen = next(item for item in effective if item["event_id"] == "manual:action-1")
    assert chosen["event_type"] == "oa_completed"
    assert chosen["source"] == "manual_adjudication"
    assert chosen["supersedes"]
finally:
    import shutil
    shutil.rmtree(tmp)

print("test_outcomes: all assertions passed")
