#!/usr/bin/env python3
"""Check a tailored artifact against the exact inputs recorded in its work item.

This is a review aid. It does not create or promote accepted records and cannot
authorize a merge or application enqueue. A prior owner acceptance can be
matched only when every input hash, the artifact bytes, and the supplied head
are identical.
"""

import argparse
import hashlib
import json
import sys

import cv_checks
import eligibility
import tailor
from posting_guard import guard_text


def _sha256(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _finding(category, code, message):
    return {"category": category, "code": code, "message": message}


def is_explicitly_closed(text):
    """Return true only for guarded, direct closure wording."""
    return tailor.is_explicitly_closed(text)


def _parse_profile(profile_bytes):
    return tailor.parse_yaml_map(profile_bytes.decode("utf-8"))


def _input_hashes(master_bytes, profile_bytes, posting, jd_text, profile):
    decision = eligibility.evaluate(posting, profile, jd_text, profile_bytes)
    provenance = decision.get("provenance", {})
    hashes = {
        "master_sha256": _sha256(master_bytes),
        "profile_sha256": _sha256(profile_bytes),
        "jd_sha256": provenance.get("jd_sha256"),
        "role_title_sha256": provenance.get("posting_metadata_sha256"),
        "combined_text_sha256": provenance.get("evaluated_text_sha256"),
    }
    return hashes, decision


def _reuse_status(record, hashes, artifact_sha256, head):
    """Validate an already owner-accepted record without creating one."""
    if record is None:
        return "review_required", [_finding(
            "staleness", "reuse_evidence_missing",
            "No prior owner-accepted check record was supplied.")]
    if (not isinstance(record, dict) or record.get("schema_version") != 1 or
            record.get("record_type") != "owner_accepted_preflight" or
            record.get("accepted") is not True or
            not isinstance(record.get("accepted_by"), str) or not record["accepted_by"].strip() or
            not isinstance(record.get("accepted_at"), str) or not record["accepted_at"].strip()):
        return "review_required", [_finding(
            "staleness", "reuse_evidence_invalid",
            "Prior acceptance evidence is missing required owner fields.")]

    findings = []
    if record.get("input_hashes") != hashes:
        findings.append(_finding("staleness", "stale_inputs",
                                 "Prior acceptance input hashes do not match current inputs."))
    if record.get("artifact_sha256") != artifact_sha256:
        findings.append(_finding("staleness", "artifact_content_mismatch",
                                 "Artifact bytes differ from the prior accepted artifact."))
    if not head or record.get("head") != head:
        findings.append(_finding("staleness", "artifact_head_mismatch",
                                 "Artifact head differs from the prior accepted head."))
    if findings:
        return "review_required", findings
    return "exact_prior_acceptance", []


def check_artifact(work_item, master_bytes, profile_bytes, artifact_bytes, head,
                   accepted_record=None, layout_checker=None):
    """Return content-free findings and exact input hashes for review.

    ``layout_checker`` is an optional callable receiving artifact bytes and
    returning a list of stable findings. An absent or failing checker is
    visible as infrastructure/build evidence, never as a clean layout result.
    """
    findings = []
    artifact_sha256 = _sha256(artifact_bytes) if isinstance(artifact_bytes, bytes) else ""
    if not isinstance(work_item, dict):
        return {"schema_version": 1, "status": "review_required", "findings": [
            _finding("infrastructure", "work_item_invalid", "Work item is not a JSON object.")],
            "input_hashes": {}, "artifact_sha256": artifact_sha256, "head": head or ""}
    if not isinstance(master_bytes, bytes) or not isinstance(profile_bytes, bytes):
        missing = []
        if not isinstance(master_bytes, bytes):
            missing.append("master")
        if not isinstance(profile_bytes, bytes):
            missing.append("profile")
        return {"schema_version": 1, "status": "review_required", "findings": [
            _finding("infrastructure", "input_unavailable",
                     "Current input bytes are unavailable: " + ", ".join(missing) + ".")],
            "input_hashes": {}, "artifact_sha256": artifact_sha256, "head": head or ""}
    if not isinstance(artifact_bytes, bytes):
        return {"schema_version": 1, "status": "review_required", "findings": [
            _finding("infrastructure", "artifact_unavailable",
                     "Generated artifact bytes are unavailable.")],
            "input_hashes": {}, "artifact_sha256": "", "head": head or ""}

    try:
        profile = _parse_profile(profile_bytes)
        posting = work_item.get("eligibility_posting")
        if not isinstance(posting, dict):
            posting = {"role": work_item.get("role", ""),
                       "title": work_item.get("title", "")}
        guarded_posting = dict(posting)
        metadata_findings = []
        for key in ("role", "title"):
            result = guard_text(str(guarded_posting.get(key) or ""))
            guarded_posting[key] = result["clean_text"]
            metadata_findings.extend(result["findings"])
        jd_guard = guard_text(str(work_item.get("jd_text") or ""))
        guarded_jd = jd_guard["clean_text"]
        if jd_guard["verdict"] == "hostile" or any(
                finding.get("kind") == "injection" for finding in metadata_findings):
            findings.append(_finding("content", "posting_guard_hostile",
                                     "Guarded posting input contains hostile instructions."))
        closure_evidence = tailor.closed_posting_evidence(
            "\n".join((guarded_posting.get("role", ""),
                      guarded_posting.get("title", ""), guarded_jd)))
        if closure_evidence:
            findings.append(_finding("closure", "posting_closed",
                                     "Guarded posting text contains explicit closure evidence: "
                                     + closure_evidence))
        input_hashes, decision = _input_hashes(
            master_bytes, profile_bytes, guarded_posting, guarded_jd, profile)
    except (UnicodeDecodeError, TypeError, ValueError, KeyError) as exc:
        findings.append(_finding("infrastructure", "input_read_failed",
                                 "Could not read current preflight inputs (%s)." % type(exc).__name__))
        input_hashes, decision = {}, {}

    if input_hashes:
        saved = work_item.get("input_provenance")
        if not isinstance(saved, dict):
            findings.append(_finding("staleness", "input_provenance_missing",
                                     "Work item has no input provenance to compare."))
        else:
            for key, current in input_hashes.items():
                if saved.get(key) != current:
                    findings.append(_finding("staleness", "stale_inputs",
                                             "Current %s does not match the work item." % key))
        prior_decision = work_item.get("eligibility")
        if (not isinstance(prior_decision, dict) or
                prior_decision.get("status") != decision.get("status") or
                prior_decision.get("reason_codes") != decision.get("reason_codes") or
                prior_decision.get("provenance") != decision.get("provenance")):
            findings.append(_finding("staleness", "eligibility_record_mismatch",
                                     "Stored eligibility evidence differs from the current evaluation."))
        if decision.get("status") == "ineligible":
            findings.append(_finding("eligibility", "eligibility_conflict",
                                     "Shared eligibility evaluator found an explicit conflict."))
        elif decision.get("status") != "eligible":
            findings.append(_finding("eligibility", "eligibility_unknown",
                                     "Eligibility is unknown and needs visible review."))

    try:
        artifact_text = artifact_bytes.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        artifact_text = None
        findings.append(_finding("infrastructure", "artifact_read_failed",
                                 "Generated artifact is unavailable or not UTF-8 text."))
    try:
        master_text = master_bytes.decode("utf-8")
    except UnicodeDecodeError:
        master_text = None
        findings.append(_finding("infrastructure", "master_decode_failed",
                                 "Current master is not valid UTF-8 text."))
    if artifact_text is not None and master_text is not None:
        for item in cv_checks.check(master_text, artifact_text):
            findings.append(_finding("content", item["code"], item["message"]))

    if layout_checker is None:
        findings.append(_finding("infrastructure", "layout_checker_unavailable",
                                 "No layout checker was supplied; layout is not verified."))
    else:
        try:
            layout_findings = layout_checker(artifact_bytes)
            if not isinstance(layout_findings, list):
                findings.append(_finding("infrastructure", "layout_checker_failed",
                                         "Layout checker returned an invalid result."))
            else:
                for item in layout_findings:
                    if isinstance(item, dict) and item.get("code"):
                        code = item["code"]
                        category = ("infrastructure" if code in
                                    ("layout_checker_unavailable", "layout_tool_failed")
                                    else "build_layout")
                        findings.append(_finding(category, code,
                                                 str(item.get("message") or "Layout check needs review.")))
                    else:
                        findings.append(_finding("infrastructure", "layout_checker_failed",
                                                 "Layout checker returned a malformed finding."))
        except Exception as exc:  # the injected tool may fail for OS/process reasons
            findings.append(_finding("infrastructure", "layout_checker_failed",
                                     "Layout check failed (%s)." % type(exc).__name__))

    reuse_status, reuse_findings = _reuse_status(
        accepted_record, input_hashes, artifact_sha256, head)
    findings.extend(reuse_findings)
    if findings or reuse_status != "exact_prior_acceptance":
        status = "review_required"
    else:
        status = "exact_prior_acceptance"
    return {
        "schema_version": 1,
        "status": status,
        "input_hashes": input_hashes,
        "artifact_sha256": artifact_sha256,
        "head": head or "",
        "reuse_status": reuse_status,
        "findings": findings,
    }


def _read_optional_json(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as input_file:
        return json.load(input_file)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work-item", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--head", required=True,
                        help="exact artifact commit head to compare with prior acceptance")
    parser.add_argument("--accepted-record",
                        help="path to a prior owner-accepted record; never written or modified")
    parser.add_argument("--pdf", help="optional built PDF for the pdfinfo layout check")
    args = parser.parse_args(argv)
    try:
        work_item = _read_optional_json(args.work_item)
        accepted_record = _read_optional_json(args.accepted_record)
        with open(args.master, "rb") as source_file:
            master_bytes = source_file.read()
        with open(args.profile, "rb") as profile_file:
            profile_bytes = profile_file.read()
        with open(args.artifact, "rb") as artifact_file:
            artifact_bytes = artifact_file.read()
    except (OSError, ValueError) as exc:
        report = {"schema_version": 1, "status": "review_required", "findings": [
            _finding("infrastructure", "input_read_failed",
                     "Could not read a required input (%s)." % type(exc).__name__)]}
        print(json.dumps(report, indent=2))
        return 2

    layout_checker = None
    if args.pdf:
        layout_checker = lambda _artifact: cv_checks.check_pdf(args.pdf)
    report = check_artifact(work_item, master_bytes, profile_bytes, artifact_bytes,
                            args.head, accepted_record, layout_checker)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "exact_prior_acceptance" else 1


if __name__ == "__main__":
    raise SystemExit(main())
