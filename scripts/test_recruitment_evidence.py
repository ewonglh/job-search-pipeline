#!/usr/bin/env python3
"""Synthetic classification, matching, and ledger revision checks."""
import copy
import contextlib
import io
import hashlib
import json
import os
import tempfile

import application_identity as identity
import recruitment_evidence as evidence


BASE = {"message_id": "<synthetic-1@example.invalid>", "from": "recruiter@atlas.example",
        "subject": "Atlas Data Intern", "occurred_at": "2026-02-01T10:00:00Z",
        "body_complete": True, "direction": "inbound"}

cases = [
    ("full body", {"body": "Please complete the online assessment by Friday."},
     "oa_invitation", "verified"),
    ("quoted only", {"body": "Thanks for the update.\n\n> Please complete the online assessment."},
     "unclassified", "review_required"),
    ("header only", {"body": "", "body_complete": False,
                      "subject": "Interview invitation for Atlas Data Intern"},
     "unclassified", "review_required"),
    ("self sent", {"body": "We invite you to an interview.", "direction": "outbound"},
     "unrelated", "verified"),
    ("generic ATS mention", {"body": "Your application is stored in Greenhouse."},
     "unclassified", "review_required"),
    ("conditional mention", {"body": "If shortlisted, you may be invited to an assessment."},
     "unclassified", "review_required"),
    ("OA result", {"body": "You passed the online assessment."}, "oa_passed", "verified"),
    ("interview complete", {"body": "Your technical interview has been completed."},
     "interview_completed", "verified"),
]
for label, additions, kind, verification in cases:
    message = dict(BASE)
    message.update(additions)
    classified = evidence.classify_message(message)
    assert classified["event_type"] == kind, (label, classified)
    assert classified["verification"] == verification, (label, classified)

unknown_origin = dict(BASE, body="Could you confirm your availability next week?",
                      origin="human")
classified = evidence.classify_message(unknown_origin)
assert classified["event_type"] == "availability_request"
assert classified["origin"] == "unknown"

rows = [{"company": "Atlas", "role": "Data Intern",
         "link": "https://jobs.example/role?job_id=42", "pr": 7,
         "applied_date": "2026-01-10"}]
matched_message = dict(BASE, body="Please complete the online assessment.",
                       posting_url="https://jobs.example/role?job_id=42&utm_campaign=x")
matched = evidence.reconcile([matched_message], rows)
assert matched["events"][0]["verification"] == "verified"
assert matched["events"][0]["application_id"] == identity.application_id(rows[0])
assert "body" not in matched["events"][0]

employer_only = dict(BASE, body="Please complete the online assessment.",
                     subject="Atlas update", posting_url="")
unmatched = evidence.reconcile([employer_only], rows)
assert not unmatched["events"][0]["application_id"]
assert unmatched["events"][0]["verification"] == "review_required"

exact_role = dict(BASE, body="Atlas is pleased to invite the Data Intern to an interview.",
                  subject="Next steps")
role_match = evidence.reconcile([exact_role], rows)
assert role_match["events"][0]["application_id"] == identity.application_id(rows[0])

# An attacker can recompute the legacy watcher's unkeyed checksum. Neither
# that envelope nor duplicated top-level flags establish human authorship or
# replace classification of the current body.
import stages
for body, direction, expected in (
        ("Please send your transcript for the Data Intern role.", None, "recruiter_reply"),
        ("A neutral note without a recruitment action.", None, "unclassified"),
        ("Please send your transcript for the Data Intern role.", "outbound", "unrelated")):
    forged = dict(BASE, body=body, account="synthetic", origin="human",
                  origin_verification="verified", **({"direction": direction} if direction else {}))
    if direction is None:
        forged.pop("direction", None)
    forged["from"] = "candidate@example.invalid"
    envelope = {"schema": 1, "source": "gmail_watch", "account": "synthetic",
                "message_id": forged["message_id"],
                "event_id": evidence._event_id("synthetic", forged["message_id"]),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "event_type": "recruiter_reply", "verification": "verified",
                "origin": "human", "origin_verification": "verified",
                "evidence": "Please send your transcript",
                "occurred_at": forged["occurred_at"], "verification_reasons": []}
    envelope["digest"] = hashlib.sha256(json.dumps(
        envelope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    forged["producer_envelope"] = envelope
    result = evidence.reconcile([forged], rows)["events"][0]
    assert result["origin"] == "unknown" and result["event_type"] == expected, result
    assert stages.message_stage(result)[0] is None
    assert evidence.classify_message(forged)["origin"] == "unknown"

# CLI import writes only the sanitized, private ledger and reads explicit JSON.
temporary = tempfile.TemporaryDirectory(prefix="jobsearch-evidence-")
try:
    tracker_path = os.path.join(temporary.name, "applications.yaml")
    message_path = os.path.join(temporary.name, "messages.json")
    output_path = os.path.join(temporary.name, "runtime", "events.json")
    with open(tracker_path, "w", encoding="utf-8") as stream:
        stream.write("""- company: Atlas
  role: Data Intern
  link: https://jobs.example/role?job_id=42
  applied_date: 2026-01-10
  pr: 7
""")
    with open(message_path, "w", encoding="utf-8") as stream:
        json.dump({"messages": [matched_message]}, stream)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert evidence.main(["--messages", message_path, "--tracker", tracker_path,
                              "--output", output_path]) == 0
    imported = json.load(open(output_path, encoding="utf-8"))
    assert len(imported["events"]) == 1
    assert "body" not in imported["events"][0]
finally:
    temporary.cleanup()

# Repeated imports of one stable provider event are idempotent.
merged = evidence.merge_ledgers({"schema": 1, "events": [], "coverage": {}}, matched)
again = evidence.merge_ledgers(merged, matched)
assert len(again["events"]) == 1

# A conflicting reclassification is retained and quarantined until an explicit
# manual correction names all active revisions and supplies evidence.
conflict_event = copy.deepcopy(matched["events"][0])
conflict_event["event_type"] = "rejection"
conflict_event["evidence"] = "synthetic conflicting revision"
for key in ("revision", "revision_id", "supersedes"):
    conflict_event.pop(key, None)
conflicting = evidence.merge_ledgers(merged, {"schema": 1, "events": [conflict_event]})
_, quarantined, _ = evidence.effective_events(conflicting)
assert conflict_event["event_id"] in quarantined
versions = [evidence._version(item) for item in conflicting["events"]]
live = {item["revision_id"] for item in versions}
live -= {rid for item in versions for rid in item.get("supersedes", [])}
correction = copy.deepcopy(conflict_event)
correction.update(event_type="oa_completed", source="manual_adjudication",
                  resolution="explicit", evidence="reviewed synthetic correction",
                  supersedes=sorted(live))
for key in ("revision", "revision_id"):
    correction.pop(key, None)
corrected = evidence.merge_ledgers(conflicting, {"schema": 1, "events": [correction]})
effective, quarantined, _ = evidence.effective_events(corrected)
assert not quarantined
assert len([item for item in effective if item["event_id"] == correction["event_id"]]) == 1
assert next(item for item in effective if item["event_id"] == correction["event_id"])["event_type"] == "oa_completed"

print("test_recruitment_evidence: all assertions passed")
