#!/usr/bin/env python3
"""Synthetic contract checks for private submission snapshots."""
import json
import os
import subprocess
import tempfile

import submission_snapshot as snapshots


APP = "link:https://fixture.invalid/jobs/12"
ATTEMPT = "attempt-12"
VERIFICATION = "I checked the selected PDF and answers for this posting."
COMMITS = {
    "resume_repository_head": {"status": "unknown", "sha": None},
    "variant_source": {"status": "unknown", "sha": None},
    "master_source": {"status": "unknown", "sha": None},
}


def expect_error(fn, text):
    try:
        fn()
    except snapshots.SnapshotError as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError("expected SnapshotError containing: " + text)


def material(root, pdf=b"%PDF-1.7\nsynthetic resume\n",
             answers=b'{"availability":"May to August"}\n'):
    pdf_file = os.path.join(root, "selected.pdf")
    answers_file = os.path.join(root, "selected.json")
    with open(pdf_file, "wb") as stream:
        stream.write(pdf)
    with open(answers_file, "wb") as stream:
        stream.write(answers)
    return pdf_file, answers_file


def _write(path, data):
    with open(path, "wb") as stream:
        stream.write(data)
    return path


def legacy_fixture(root, *, hash_only=False, pretty=False):
    os.makedirs(root, exist_ok=True)
    manifests = os.path.join(root, "manifests")
    os.makedirs(manifests, exist_ok=True)
    pdf = b"%PDF-1.7\nsynthetic legacy resume\n"
    answers = b'{"availability":"synthetic summer term"}\n'
    if not hash_only:
        os.makedirs(os.path.join(root, "pdf"), exist_ok=True)
        os.makedirs(os.path.join(root, "answers"), exist_ok=True)
        _write(os.path.join(root, "pdf", snapshots.sha256(pdf) + ".pdf"), pdf)
        _write(os.path.join(root, "answers", snapshots.sha256(answers) + ".json"), answers)
        manifest = {
            "schema_version": 1,
            "captured_at": "2026-11-22T10:00:00Z",
            "posting": {"id": "link:https://fixture.invalid/jobs/13",
                        "url": "https://fixture.invalid/jobs/13"},
            "pr_number": 13,
            "variant": "variants/synthetic-role",
            "artifacts": {
                "resume_pdf": {"sha256": snapshots.sha256(pdf)},
                "answers_json": {"sha256": snapshots.sha256(answers)},
            },
            "submitted_date": "2026-11-22",
            "submission_recorded_at": "2026-11-22T10:05:00Z",
            "confirmation_evidence": "Synthetic receipt confirmed.",
            "provenance": {"kind": "synthetic"},
        }
    else:
        manifest = {
            "schema_version": 1,
            "captured_at": "2026-11-22T10:00:00Z",
            "canonical_application_id": "pr:13",
            "attempt_id": "legacy-attempt-13",
            "confirmation": {"submitted_at": "2026-11-22",
                             "evidence": "Synthetic receipt confirmed."},
            "artifacts": {"resume_pdf": {"sha256": snapshots.sha256(pdf),
                                           "path": "ignored legacy location"}},
        }
    canonical = snapshots._manifest_bytes(manifest)
    manifest_id = snapshots.sha256(canonical)
    data = (json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                       indent=2).encode("utf-8") + b"\n"
            if pretty else canonical)
    _write(os.path.join(manifests, manifest_id + ".json"), data)
    return manifest_id, manifest, pdf, answers


with tempfile.TemporaryDirectory() as temp:
    public = os.path.join(temp, "public-checkout")
    private = os.path.join(temp, "private-checkout")
    for checkout in (public, private):
        expect_error(lambda: snapshots.ensure_external_storage(
            os.path.join(checkout, ".private", "snapshots"), public, private),
            "outside Git checkouts")
    outside = os.path.join(temp, "state", "snapshots")
    assert snapshots.ensure_external_storage(outside, public, private) == os.path.realpath(outside)


with tempfile.TemporaryDirectory() as temp:
    repo = os.path.join(temp, "synthetic-resume-repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", repo], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", repo, "-c", "user.name=Synthetic Test",
                    "-c", "user.email=synthetic@example.invalid", "commit",
                    "--allow-empty", "-m", "synthetic source commit"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"],
                                   text=True).strip()
    commits = snapshots.source_commits(
        repo, {"variant_commit": head, "master_commit": "0" * 40})
    assert commits["resume_repository_head"] == {"status": "verified", "sha": head}
    assert commits["variant_source"] == {"status": "verified", "sha": head}
    assert commits["master_source"] == {"status": "unknown", "sha": None}


with tempfile.TemporaryDirectory() as temp:
    pdf_file, answers_file = material(temp)
    root = os.path.join(temp, "private", "snapshots")
    prepared = snapshots.prepare_snapshot(pdf_file, answers_file, root, APP, ATTEMPT,
                                          VERIFICATION, COMMITS)
    assert prepared["pdf_sha256"] == snapshots.sha256(open(pdf_file, "rb").read())
    assert prepared["answers_sha256"] == snapshots.sha256(open(answers_file, "rb").read())
    manifest = snapshots.verify_snapshot(root, prepared["snapshot_id"], APP,
                                         ATTEMPT, pdf_file, answers_file)
    manifest_path = os.path.join(root, "manifests", prepared["snapshot_id"] + ".json")
    canonical_manifest = open(manifest_path, "rb").read()
    _write(manifest_path, json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8"))
    expect_error(lambda: snapshots.verify_snapshot(root, prepared["snapshot_id"],
                                                    APP, ATTEMPT), "encoding")
    _write(manifest_path, canonical_manifest)
    current_facts = snapshots.read_snapshot_manifest(root, prepared["snapshot_id"])
    assert current_facts["snapshot_kind"] == "current"
    assert current_facts["attempt_id"] == ATTEMPT
    assert current_facts["attempt_provenance"] == "verified"
    assert manifest["canonical_application_id"] == APP
    assert manifest["attempt_id"] == ATTEMPT
    assert manifest["confirmation"] == {"submitted_at": None, "evidence": None}
    assert all(row["status"] == "unknown" for row in manifest["source_commits"].values())
    finalized = snapshots.finalize_snapshot(
        root, prepared["snapshot_id"], APP, ATTEMPT, "2026-11-16",
        "Employer portal confirmed receipt.")
    final_manifest = snapshots.verify_snapshot(root, finalized, APP, ATTEMPT)
    assert final_manifest["confirmation"] == {
        "submitted_at": "2026-11-16",
        "evidence": "Employer portal confirmed receipt.",
    }
    assert final_manifest["artifacts"] == manifest["artifacts"]

    # Changed selected materials and cross-attempt/application reuse fail closed.
    changed_pdf = b"%PDF-1.7\nchanged synthetic resume\n"
    changed_answers = b'{"availability":"June to September"}\n'

    def verify_changed_answers():
        _write(pdf_file, b"%PDF-1.7\nsynthetic resume\n")
        _write(answers_file, changed_answers)
        return snapshots.verify_snapshot(root, prepared["snapshot_id"], APP,
                                         ATTEMPT, pdf_file, answers_file)

    mismatch_cases = [
        ("changed PDF", lambda: snapshots.verify_snapshot(
            root, prepared["snapshot_id"], APP, ATTEMPT,
            _write(pdf_file, changed_pdf), answers_file), "changed"),
        ("changed answers", verify_changed_answers, "changed"),
        ("different application", lambda: snapshots.verify_snapshot(
            root, prepared["snapshot_id"], "link:https://fixture.invalid/jobs/99",
            ATTEMPT), "another application"),
        ("different attempt", lambda: snapshots.verify_snapshot(
            root, prepared["snapshot_id"], APP, "attempt-99"), "another application"),
    ]
    for label, check, expected in mismatch_cases:
        try:
            check()
        except snapshots.SnapshotError as exc:
            assert expected in str(exc), (label, str(exc))
        else:
            raise AssertionError(label + " unexpectedly verified")
    expect_error(lambda: snapshots.verify_snapshot(root, "not-a-hash", APP, ATTEMPT),
                 "snapshot ID")

with tempfile.TemporaryDirectory() as temp:
    root = os.path.join(temp, "private")
    invalid_answers = (
            (b'{"nested":{"field":"OTP","answer":"SENSITIVE_SYNTHETIC_SENTINEL"}}',
             "SENSITIVE_SYNTHETIC_SENTINEL"),
            (b'{"question":"One-time code","answer":"SENSITIVE_SYNTHETIC_SENTINEL"}',
             "SENSITIVE_SYNTHETIC_SENTINEL"),
            (b'{"nested":{"cookie":"SENSITIVE_SYNTHETIC_SENTINEL"}}',
             "SENSITIVE_SYNTHETIC_SENTINEL"),
            b'{"availability":"May","availability":"June"}',
            b'{not json',
            b'"not an answer object"',
    )
    for invalid in invalid_answers:
        answers, secret = invalid if isinstance(invalid, tuple) else (invalid, "")
        pdf_file, answers_file = material(temp, answers=answers)
        try:
            snapshots.prepare_snapshot(pdf_file, answers_file, root, APP, ATTEMPT,
                                       VERIFICATION, COMMITS)
        except snapshots.SnapshotError as exc:
            assert not secret or secret not in str(exc)
        else:
            raise AssertionError("unsafe or malformed answers were accepted")

with tempfile.TemporaryDirectory() as temp:
    pdf_file, answers_file = material(temp)
    root = os.path.join(temp, "private")
    prepared = snapshots.prepare_snapshot(pdf_file, answers_file, root, APP, ATTEMPT,
                                          VERIFICATION, COMMITS)
    pdf_link = os.path.join(temp, "selected-link.pdf")
    try:
        os.symlink(pdf_file, pdf_link)
    except (OSError, NotImplementedError):
        pass
    else:
        expect_error(lambda: snapshots.verify_snapshot(root, prepared["snapshot_id"],
                                                         APP, ATTEMPT, pdf_link, answers_file),
                     "symlink")
    manifest_path = os.path.join(root, "manifests", prepared["snapshot_id"] + ".json")
    with open(manifest_path, "rb") as stream:
        original = json.loads(stream.read().decode("utf-8"))
    original["artifacts"]["resume_pdf"]["path"] = "../outside.pdf"
    data = snapshots._manifest_bytes(original)
    hostile_id = snapshots.sha256(data)
    with open(os.path.join(root, "manifests", hostile_id + ".json"), "wb") as stream:
        stream.write(data)
    expect_error(lambda: snapshots.verify_snapshot(root, hostile_id, APP, ATTEMPT),
                 "artifact metadata")
    with open(os.path.join(root, "manifests", "0" * 64 + ".json"), "wb") as stream:
        stream.write(b'{"schema_version":1,"schema_version":1}')
    expect_error(lambda: snapshots.verify_snapshot(root, "0" * 64, APP, ATTEMPT),
                 "duplicate")
    artifact = original["artifacts"]["answers_json"]
    blob_path = os.path.join(root, "answers", artifact["sha256"] + ".json")
    os.chmod(blob_path, 0o600)
    with open(blob_path, "wb") as stream:
        stream.write(b"corrupt")
    expect_error(lambda: snapshots.verify_snapshot(root, prepared["snapshot_id"],
                                                     APP, ATTEMPT), "retained snapshot bytes")

with tempfile.TemporaryDirectory() as temp:
    root = os.path.join(temp, "legacy-snapshots")
    manifest_id, raw, pdf, answers = legacy_fixture(root)
    facts = snapshots.read_snapshot_manifest(root, manifest_id)
    assert facts["snapshot_kind"] == "legacy"
    assert facts["application_id"] == "link:https://fixture.invalid/jobs/13"
    assert facts["attempt_id"] is None
    assert facts["attempt_provenance"] == "unknown"
    assert facts["artifact_provenance"] == "hash_verified"
    assert facts["resume_hash"] == snapshots.sha256(pdf)
    assert facts["answers_hash"] == snapshots.sha256(answers)
    assert facts["submitted_at"] == "2026-11-22"
    expect_error(lambda: snapshots.verify_snapshot(root, manifest_id,
                                                    facts["application_id"], "legacy-13"),
                 "attempt provenance is unknown")
    _write(os.path.join(root, "answers", facts["answers_hash"] + ".json"), b"corrupt")
    expect_error(lambda: snapshots.read_snapshot_manifest(root, manifest_id),
                 "retained legacy snapshot bytes")

with tempfile.TemporaryDirectory() as temp:
    root = os.path.join(temp, "legacy-hash-only")
    manifest_id, _raw, pdf, _answers = legacy_fixture(root, hash_only=True)
    facts = snapshots.read_snapshot_manifest(root, manifest_id)
    assert facts["snapshot_kind"] == "legacy_hash_only"
    assert facts["application_id"] == "pr:13"
    assert facts["attempt_id"] is None
    assert facts["attempt_provenance"] == "unknown"
    assert facts["artifact_provenance"] == "manifest_only"
    assert facts["resume_hash"] == snapshots.sha256(pdf)
    expect_error(lambda: snapshots.verify_snapshot(root, manifest_id, "pr:13",
                                                    "legacy-attempt-13"),
                 "attempt provenance is unknown")

with tempfile.TemporaryDirectory() as temp:
    root = os.path.join(temp, "legacy-formatted-snapshots")
    manifest_id, _raw, _pdf, _answers = legacy_fixture(root, pretty=True)
    facts = snapshots.read_snapshot_manifest(root, manifest_id)
    assert facts["snapshot_kind"] == "legacy"
    assert facts["attempt_provenance"] == "unknown"
    assert facts["artifact_provenance"] == "hash_verified"

print("test_submission_snapshot: prepare, binding, retained bytes, current materials, and rejection cases passed")
