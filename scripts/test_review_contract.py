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
PROFILE_HASH = "5" * 64
ARTIFACTS = {
    "variants/example-role/resume.tex": "6" * 64,
    "variants/example-role/resume.pdf": "7" * 64,
}
POSTING_EVIDENCE = contract.posting_evidence(
    {"company": "Example Co", "role": "Software Intern",
     "link": "https://example.invalid/jobs/example-role"},
    "Synthetic description for a software internship.")


def fixture():
    provenance = {
        "schema": contract.SCHEMA,
        "review_head_sha": HEAD,
        "master_commit": MASTER,
        "master_resume_blob": MASTER_BLOB,
        "master_resume_path": "master/resume.tex",
        **POSTING_EVIDENCE,
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
        "current_jd_sha256": POSTING_EVIDENCE["current_jd_sha256"],
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
provenance_result = contract.valid_provenance(entry["provenance"], HEAD)
assert provenance_result == (True, "valid current posting evidence"), provenance_result
assert contract.approval_result(entry, HEAD) == (True, "valid current-head review")
assert contract.approval_valid(entry, HEAD)

# Legacy approvals remain readable as historical model decisions, but their
# provenance is not current and they cannot satisfy readiness without review.
legacy = fixture()
legacy["provenance"].pop("posting_evidence_version")
legacy["provenance"].pop("posting_identity")
legacy["audit"]["provenance_sha256"] = contract.canonical_sha256(legacy["provenance"])
assert contract.valid_historical_provenance(legacy["provenance"], HEAD) == (True, "valid")
assert not contract.valid_provenance(legacy["provenance"], HEAD)[0]
assert not contract.approval_valid(legacy, HEAD)
assert contract.historical_approval_result(legacy, HEAD)[0]

unsupported = fixture()
unsupported["provenance"]["posting_evidence_version"] += 1
unsupported["audit"]["provenance_sha256"] = contract.canonical_sha256(
    unsupported["provenance"])
assert not contract.valid_provenance(unsupported["provenance"], HEAD)[0]
assert not contract.approval_valid(unsupported, HEAD)

bad_identity = fixture()
bad_identity["provenance"]["posting_identity"] = "link:https://EXAMPLE.invalid/jobs/example-role/"
bad_identity["audit"]["provenance_sha256"] = contract.canonical_sha256(
    bad_identity["provenance"])
assert "canonical" in contract.valid_provenance(bad_identity["provenance"], HEAD)[1]

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
