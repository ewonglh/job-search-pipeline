#!/usr/bin/env python3
"""Synthetic checks for the review provenance contract."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import review_contract as contract

HEAD = "1" * 40
MASTER = "2" * 40
MASTER_BLOB = "3" * 40
JD_HASH = "4" * 64
PROFILE_HASH = "5" * 64
ARTIFACTS = {
    "variants/example-role/resume.tex": "6" * 64,
    "variants/example-role/resume.pdf": "7" * 64,
}


def fixture():
    provenance = {
        "schema": contract.SCHEMA,
        "review_head_sha": HEAD,
        "master_commit": MASTER,
        "master_resume_blob": MASTER_BLOB,
        "master_resume_path": "master/resume.tex",
        "current_jd_sha256": JD_HASH,
        "profile_sha256": PROFILE_HASH,
        "artifacts": dict(ARTIFACTS),
    }
    audit = {
        "truthfulness": "pass",
        "eligibility": "eligible",
        "layout_checked": True,
        "onepage_checked": True,
        "no_tracker_edit": True,
        "findings": [{"severity": "minor", "message": "One date format is abbreviated."}],
        "summary": "The synthetic resume matches the role and contains no blocking issue.",
        "posting_url": "https://example.invalid/jobs/example-role",
        "current_jd_sha256": JD_HASH,
        "profile_sha256": PROFILE_HASH,
        "master_commit": MASTER,
        "master_resume_blob": MASTER_BLOB,
        "artifact_sha256": dict(ARTIFACTS),
        "checked_at": "2026-11-15T00:00:00Z",
    }
    audit["provenance_sha256"] = contract.canonical_sha256(provenance)
    return {
        "verdict": "approve",
        "review_head_sha": HEAD,
        "reviewer_version": "synthetic-reviewer/1",
        "provenance": provenance,
        "audit": audit,
    }


entry = fixture()
assert contract.valid_provenance(entry["provenance"], HEAD) == (True, "valid")
assert contract.approval_result(entry, HEAD) == (True, "valid current-head review")
assert contract.approval_valid(entry, HEAD)

# A changed PR head invalidates otherwise complete review evidence.
valid, reason = contract.approval_result(entry, "8" * 40)
assert not valid and "stale" in reason, reason

# Changed artifact evidence or an invalid digest cannot keep the approval valid.
changed = fixture()
changed["provenance"]["artifacts"]["variants/example-role/resume.tex"] = "9" * 64
valid, reason = contract.approval_result(changed, HEAD)
assert not valid and "audit" in reason, reason

bad_hash = fixture()
bad_hash["provenance"]["artifacts"]["variants/example-role/resume.pdf"] = "not-a-sha256"
valid, reason = contract.approval_result(bad_hash, HEAD)
assert not valid and "artifact hash" in reason, reason

bad_audit = fixture()
bad_audit["audit"]["findings"] = [{"severity": "blocking", "message": "Synthetic blocker."}]
assert not contract.approval_valid(bad_audit, HEAD)

print("test_review_contract: provenance, current-head binding, and structured findings passed")
