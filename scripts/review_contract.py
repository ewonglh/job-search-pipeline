#!/usr/bin/env python3
"""Validate a structured review against one immutable application snapshot.

This module checks the shape and linkage of review evidence. It does not make
the reviewer decision, contact a model, or authorize a GitHub merge.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit
from datetime import datetime
from application_identity import application_id, normalize_posting_url
from posting_guard import guard_text

SCHEMA = 1
POSTING_EVIDENCE_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_RE = re.compile(
    r"^variants/[A-Za-z0-9][A-Za-z0-9._-]*/"
    r"(?:resume\.(?:tex|pdf|txt)|cover[_-]letter\.md)$"
)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value):
    """Hash JSON data using stable UTF-8 encoding and key ordering."""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return sha256_bytes(data)


def posting_evidence(posting, jd_text):
    """Create the requisition and guarded JD fields shared by review and readiness."""
    if not normalize_posting_url(posting.get("link")):
        raise ValueError("review posting link is unavailable")
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("current job description is unavailable")
    guarded = guard_text(jd_text)
    if guarded.get("verdict") == "hostile" or not guarded["clean_text"].strip():
        raise ValueError("current job description requires review")
    return {
        "posting_evidence_version": POSTING_EVIDENCE_VERSION,
        "posting_identity": application_id(posting),
        "current_jd_sha256": sha256_bytes(guarded["clean_text"].encode("utf-8")),
    }


def valid_historical_provenance(provenance, head_sha):
    """Validate the original immutable review fields, including legacy records."""
    if not isinstance(provenance, dict):
        return False, "provenance is missing or malformed"
    if type(provenance.get("schema")) is not int or provenance["schema"] != SCHEMA:
        return False, "unsupported provenance schema"
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return False, "current PR head is unavailable"
    if provenance.get("review_head_sha") != head_sha:
        return False, "review provenance is for a stale PR head"
    for key in ("master_commit", "master_resume_blob"):
        if not isinstance(provenance.get(key), str) or not SHA_RE.fullmatch(provenance[key]):
            return False, "provenance has an invalid " + key
    if provenance.get("master_resume_path") != "master/resume.tex":
        return False, "provenance has an unexpected master resume path"
    for key in ("current_jd_sha256", "profile_sha256"):
        if not isinstance(provenance.get(key), str) or not SHA256_RE.fullmatch(provenance[key]):
            return False, "provenance has an invalid " + key
    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return False, "provenance has no artifact hashes"
    for path, digest in artifacts.items():
        if not isinstance(path, str) or not ARTIFACT_RE.fullmatch(path):
            return False, "provenance has an invalid artifact path"
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            return False, "provenance has an invalid artifact hash"
    if not any(path.endswith("/resume.tex") for path in artifacts):
        return False, "provenance does not hash resume.tex"
    if not any(path.endswith("/resume.pdf") for path in artifacts):
        return False, "provenance does not hash resume.pdf"
    return True, "valid"


def valid_current_posting_evidence(provenance):
    """Require a supported posting-evidence version and canonical identity."""
    if not isinstance(provenance, dict):
        return False, "provenance is missing or malformed"
    version = provenance.get("posting_evidence_version")
    if type(version) is not int or version != POSTING_EVIDENCE_VERSION:
        return False, "current posting evidence is missing or unsupported"
    identity = provenance.get("posting_identity")
    if not isinstance(identity, str):
        return False, "current posting identity is missing or malformed"
    if identity.startswith("link:"):
        url = identity[len("link:"):]
        if not url or normalize_posting_url(url) != url:
            return False, "current posting identity is not canonical"
        return True, "valid current posting evidence"
    if identity.startswith("requisition:"):
        raw = identity[len("requisition:"):]
        try:
            parts = json.loads(raw)
        except (TypeError, ValueError):
            return False, "current posting identity is malformed"
        if (not isinstance(parts, list) or len(parts) != 2
                or any(not isinstance(value, str) or not value.strip() for value in parts)
                or parts[0] != " ".join(parts[0].split()).casefold()
                or parts[1] != parts[1].strip()
                or json.dumps(parts, separators=(",", ":")) != raw):
            return False, "current posting identity is not canonical"
        return True, "valid current posting evidence"
    return False, "current posting identity has an unsupported form"


def valid_provenance(provenance, head_sha):
    """Validate historical fields and the current versioned posting evidence."""
    valid, reason = valid_historical_provenance(provenance, head_sha)
    if not valid:
        return False, reason
    return valid_current_posting_evidence(provenance)


def audit_eligible(audit, provenance):
    """Check structured reviewer findings and their provenance binding."""
    if not isinstance(audit, dict):
        return False
    if audit.get("truthfulness") != "pass":
        return False
    if audit.get("eligibility") not in ("eligible", "unknown"):
        return False
    if (audit.get("eligibility") == "unknown"
            and (not isinstance(audit.get("eligibility_evidence_resolves_ambiguity"), str)
                 or not audit["eligibility_evidence_resolves_ambiguity"].strip())):
        return False
    if not all(audit.get(key) is True for key in
               ("layout_checked", "onepage_checked", "no_tracker_edit")):
        return False
    findings = audit.get("findings")
    if not isinstance(findings, list):
        return False
    allowed = {"info", "informational", "minor", "moderate", "major",
               "blocker", "blocking"}
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        severity, message = finding.get("severity"), finding.get("message")
        if not isinstance(severity, str) or severity.lower() not in allowed:
            return False
        if not isinstance(message, str) or not message.strip():
            return False
        if severity.lower() in {"major", "blocker", "blocking"}:
            return False
    if not isinstance(audit.get("summary"), str) or not audit["summary"].strip():
        return False
    posting_url = audit.get("posting_url")
    try:
        parsed = urlsplit(posting_url) if isinstance(posting_url, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    for key in ("current_jd_sha256", "profile_sha256"):
        if audit.get(key) != provenance.get(key):
            return False
    if audit.get("master_commit") != provenance.get("master_commit"):
        return False
    if audit.get("master_resume_blob") != provenance.get("master_resume_blob"):
        return False
    if audit.get("artifact_sha256") != provenance.get("artifacts"):
        return False
    if audit.get("provenance_sha256") != canonical_sha256(provenance):
        return False
    checked_at = audit.get("checked_at")
    if not isinstance(checked_at, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", checked_at):
        return False
    try:
        datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def approval_result(entry, head_sha):
    """Return (valid, reason) for a review record at the current PR head."""
    if not isinstance(entry, dict) or entry.get("verdict") != "approve":
        return False, "review is not an approve verdict"
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return False, "current PR head is unavailable"
    if entry.get("review_head_sha") != head_sha:
        return False, "review is for a stale PR head"
    provenance = entry.get("provenance")
    valid, reason = valid_provenance(provenance, head_sha)
    if not valid:
        return False, reason
    reviewer = entry.get("reviewer_version")
    if not isinstance(reviewer, str) or not reviewer.strip():
        return False, "reviewer version is missing"
    if not audit_eligible(entry.get("audit"), provenance):
        return False, "review audit is incomplete or malformed"
    return True, "valid current-head review"


def historical_approval_result(entry, head_sha):
    """Interpret a structurally valid pre-versioned approval as historical only."""
    if not isinstance(entry, dict) or entry.get("verdict") != "approve":
        return False, "review is not an approve verdict"
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return False, "current PR head is unavailable"
    if entry.get("review_head_sha") != head_sha:
        return False, "review is for a stale PR head"
    provenance = entry.get("provenance")
    if (not isinstance(provenance, dict)
            or "posting_evidence_version" in provenance
            or "posting_identity" in provenance):
        return False, "review is not a legacy posting-evidence record"
    valid, reason = valid_historical_provenance(provenance, head_sha)
    if not valid:
        return False, reason
    reviewer = entry.get("reviewer_version")
    if not isinstance(reviewer, str) or not reviewer.strip():
        return False, "reviewer version is missing"
    if not audit_eligible(entry.get("audit"), provenance):
        return False, "review audit is incomplete or malformed"
    return True, "valid historical review; current posting evidence requires revalidation"


def approval_valid(entry, head_sha):
    return approval_result(entry, head_sha)[0]


def historical_approval_valid(entry, head_sha):
    return historical_approval_result(entry, head_sha)[0]
