#!/usr/bin/env python3
"""Private content-addressed submission material snapshots (stdlib only).

Queue integration owns the durable attempt binding. This module validates,
stores, and reads the immutable manifest and bytes; it never touches a form.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone


class SnapshotError(ValueError):
    """Unsafe, incomplete, or malformed snapshot input/store."""


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECRET_LABEL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:password|passwd|passcode|otp|one[_ -]*time[_ -]*code|"
    r"verification[_ -]*code|authenticator|credential|secret|access[_ -]*token|"
    r"refresh[_ -]*token|api[_ -]*key|session[_ -]*token|cookie|"
    r"bearer[_ -]*token|login[_ -]*token|csrf[_ -]*token|xsrf[_ -]*token|"
    r"oauth[_ -]*token|recovery[_ -]*code|multi[_ -]*factor[_ -]*code|"
    r"security[_ -]*(?:answer|code)|mfa|2fa|pin)(?:$|[^a-z0-9])", re.I)
SECRET_TEXT_RE = re.compile(
    r"(?:password|passwd|passcode|credential|secret|otp|one[- ]time code|"
    r"verification code|access token|refresh token|api key|session token|"
    r"bearer token|csrf token|xsrf token|oauth token|recovery code|"
    r"security code|cookie|pin)\s*[:=]\s*\S+",
    re.I)
LABEL_KEYS = ("field", "label", "name", "key", "question", "title", "prompt", "caption")
MANIFEST_KEYS = {
    "schema_version", "captured_at", "canonical_application_id", "attempt_id",
    "source_commits", "operator_material_verification", "confirmation", "artifacts",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _load_json(data, label):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except SnapshotError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError):
        raise SnapshotError(label + " must contain valid UTF-8 JSON")


def _check_answer_content(value):
    """Reject credentials by key, question label, or pasted labelled value."""
    if isinstance(value, dict):
        for label_key in LABEL_KEYS:
            label = value.get(label_key)
            if isinstance(label, str) and SECRET_LABEL_RE.search(label):
                raise SnapshotError("answers contain a credential-like field; remove it and retry")
        for key, child in value.items():
            if SECRET_LABEL_RE.search(str(key)):
                raise SnapshotError("answers contain a credential-like field; remove it and retry")
            _check_answer_content(child)
    elif isinstance(value, list):
        for child in value:
            _check_answer_content(child)
    elif isinstance(value, str) and SECRET_TEXT_RE.search(value):
        raise SnapshotError("answers contain a credential-like value; remove it and retry")


def _check_answer_bytes(data):
    parsed = _load_json(data, "answers file")
    if not isinstance(parsed, (dict, list)):
        raise SnapshotError("answers JSON must be an object or array")
    _check_answer_content(parsed)
    return parsed


def validate_confirmation_evidence(value):
    evidence = str(value or "").strip()
    if len(evidence) < 12 or SECRET_TEXT_RE.search(evidence):
        raise SnapshotError("confirmation evidence is invalid or contains a credential")
    return evidence


def _is_reparse(info):
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _walk_path(path, allow_missing=False):
    """Reject symlink/reparse components instead of following them."""
    absolute = os.path.abspath(os.fspath(path))
    drive, tail = os.path.splitdrive(absolute)
    current = drive + os.sep if drive else os.sep
    for component in [part for part in tail.split(os.sep) if part]:
        current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                continue
            raise SnapshotError("path does not exist")
        except OSError:
            raise SnapshotError("path cannot be inspected safely")
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise SnapshotError("symlink or reparse path is not allowed")
        if current != absolute and not stat.S_ISDIR(info.st_mode):
            raise SnapshotError("path contains a non-directory component")
    return absolute


def _read_regular_file(path, label):
    absolute = _walk_path(path)
    try:
        before = os.lstat(absolute)
        if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise SnapshotError(label + " must be a regular non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(absolute, flags)
        try:
            after = os.fstat(fd)
            if not stat.S_ISREG(after.st_mode) or (before.st_ino and after.st_ino != before.st_ino):
                raise SnapshotError(label + " changed while it was being read")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except SnapshotError:
        raise
    except OSError:
        raise SnapshotError("cannot read " + label)


def _read_materials(pdf_file, answers_file):
    pdf = _read_regular_file(pdf_file, "selected PDF")
    if not pdf.startswith(b"%PDF-"):
        raise SnapshotError("selected resume file is not a PDF")
    answers = _read_regular_file(answers_file, "selected answers")
    _check_answer_bytes(answers)
    return pdf, answers


def _private_dir(path, create=False):
    absolute = _walk_path(path, allow_missing=create)
    if create:
        try:
            os.makedirs(absolute, mode=0o700, exist_ok=True)
        except OSError:
            raise SnapshotError("cannot create private snapshot directory")
    absolute = _walk_path(absolute)
    try:
        info = os.lstat(absolute)
    except OSError:
        raise SnapshotError("snapshot directory is unavailable")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotError("snapshot path must be a directory")
    if os.name != "nt":
        try:
            os.chmod(absolute, 0o700)
        except OSError:
            raise SnapshotError("cannot set private snapshot directory permissions")
    return absolute


def ensure_external_storage(snapshot_root, public_repo, private_repo=None):
    """Require private snapshot storage outside both source checkouts."""
    requested = _walk_path(snapshot_root, allow_missing=True)
    root = os.path.realpath(requested)
    for repo in (public_repo, private_repo):
        if not repo:
            continue
        checkout = os.path.realpath(os.path.abspath(os.fspath(repo)))
        try:
            if os.path.commonpath((root, checkout)) == checkout:
                raise SnapshotError("snapshot storage must be outside Git checkouts")
        except ValueError:
            continue
    return root


def _store_bytes(directory, filename, data):
    directory = _private_dir(directory, create=True)
    path = os.path.join(directory, filename)
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temp_path, 0o400)
        try:
            # A hard link publishes only complete bytes and never overwrites.
            os.link(temp_path, path)
            if os.name != "nt":
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except FileExistsError:
            existing = _read_regular_file(path, "existing content-addressed artifact")
            if existing != data:
                raise SnapshotError("content-addressed artifact is corrupt")
        if os.name != "nt":
            os.chmod(path, 0o400)
        return path
    except SnapshotError:
        raise
    except OSError:
        raise SnapshotError("cannot atomically publish private snapshot bytes")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _manifest_bytes(manifest):
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _store_manifest(root, manifest):
    data = _manifest_bytes(manifest)
    manifest_id = sha256(data)
    directory = _private_dir(os.path.join(root, "manifests"), create=True)
    _store_bytes(directory, manifest_id + ".json", data)
    return manifest_id


def _verified_commit(repo, value):
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        return {"status": "unknown", "sha": None}
    try:
        proc = subprocess.run(["git", "-C", os.fspath(repo), "rev-parse", "--verify",
                              value + "^{commit}"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=False, text=True)
        result = proc.stdout.strip().lower()
        if proc.returncode == 0 and result == value.lower() and COMMIT_RE.fullmatch(result):
            return {"status": "verified", "sha": result}
    except (OSError, ValueError):
        pass
    return {"status": "unknown", "sha": None}


def source_commits(repo, item):
    """Record verified commit objects when locally provable, else explicit unknowns."""
    result = {"resume_repository_head": {"status": "unknown", "sha": None},
              "variant_source": {"status": "unknown", "sha": None},
              "master_source": {"status": "unknown", "sha": None}}
    try:
        proc = subprocess.run(["git", "-C", os.fspath(repo), "rev-parse", "--verify",
                              "HEAD^{commit}"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=False, text=True)
        head = proc.stdout.strip().lower()
        if proc.returncode == 0 and COMMIT_RE.fullmatch(head):
            result["resume_repository_head"] = {"status": "verified", "sha": head}
    except (OSError, ValueError):
        pass
    for role, keys in (("variant_source", ("variant_commit",)),
                       ("master_source", ("master_commit", "master_sha"))):
        candidate = next((item.get(key) for key in keys if item.get(key)), None)
        if candidate:
            result[role] = _verified_commit(repo, str(candidate))
    return result


def _valid_application_id(value):
    return (isinstance(value, str) and value.startswith(("link:http://", "link:https://"))
            and len(value) <= 2048 and not any(ord(ch) < 32 for ch in value))


def prepare_snapshot(pdf_file, answers_file, snapshot_root, canonical_application_id,
                     attempt_id, operator_verification, commits):
    """Capture explicitly selected materials in an immutable prepared manifest."""
    if not _valid_application_id(canonical_application_id):
        raise SnapshotError("canonical application identity is invalid")
    if not isinstance(attempt_id, str) or not ATTEMPT_RE.fullmatch(attempt_id):
        raise SnapshotError("durable attempt ID is invalid")
    verification = str(operator_verification or "").strip()
    if len(verification) < 12 or SECRET_TEXT_RE.search(verification):
        raise SnapshotError("operator material verification must be specific and contain no credentials")
    if not isinstance(commits, dict) or set(commits) != {
            "resume_repository_head", "variant_source", "master_source"}:
        raise SnapshotError("source commit provenance is malformed")
    for commit in commits.values():
        if (not isinstance(commit, dict) or set(commit) != {"status", "sha"}
                or commit["status"] not in ("verified", "unknown")
                or (commit["status"] == "verified"
                    and (not isinstance(commit["sha"], str)
                         or not COMMIT_RE.fullmatch(commit["sha"])))
                or (commit["status"] == "unknown" and commit["sha"] is not None)):
            raise SnapshotError("source commit provenance is malformed")
    pdf, answers = _read_materials(pdf_file, answers_file)
    root = _private_dir(snapshot_root, create=True)
    pdf_hash, answers_hash = sha256(pdf), sha256(answers)
    _store_bytes(os.path.join(root, "pdf"), pdf_hash + ".pdf", pdf)
    _store_bytes(os.path.join(root, "answers"), answers_hash + ".json", answers)
    manifest = {
        "schema_version": 1,
        "captured_at": now_utc(),
        "canonical_application_id": canonical_application_id,
        "attempt_id": attempt_id,
        "source_commits": commits,
        "operator_material_verification": verification,
        "confirmation": {"submitted_at": None, "evidence": None},
        "artifacts": {
            "resume_pdf": {"sha256": pdf_hash, "bytes": len(pdf),
                            "path": "pdf/" + pdf_hash + ".pdf"},
            "answers_json": {"sha256": answers_hash, "bytes": len(answers),
                             "path": "answers/" + answers_hash + ".json"},
        },
    }
    manifest_id = _store_manifest(root, manifest)
    return {"snapshot_id": manifest_id, "pdf_sha256": pdf_hash,
            "answers_sha256": answers_hash, "captured_at": manifest["captured_at"]}


def _read_manifest(root, manifest_id):
    if not isinstance(manifest_id, str) or not SHA256_RE.fullmatch(manifest_id):
        raise SnapshotError("snapshot ID is invalid")
    path = os.path.join(root, "manifests", manifest_id + ".json")
    data = _read_regular_file(path, "snapshot manifest")
    manifest = _load_json(data, "snapshot manifest")
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise SnapshotError("snapshot manifest shape is invalid")
    canonical = _manifest_bytes(manifest)
    if data != canonical or sha256(canonical) != manifest_id:
        raise SnapshotError("snapshot manifest hash or canonical encoding is invalid")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise SnapshotError("snapshot manifest schema is unsupported")
    if not _valid_application_id(manifest.get("canonical_application_id")):
        raise SnapshotError("snapshot manifest application identity is invalid")
    if not ATTEMPT_RE.fullmatch(str(manifest.get("attempt_id") or "")):
        raise SnapshotError("snapshot manifest attempt binding is invalid")
    try:
        datetime.strptime(manifest.get("captured_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        raise SnapshotError("snapshot manifest capture time is invalid")
    verification = manifest.get("operator_material_verification")
    if (not isinstance(verification, str) or len(verification) < 12
            or SECRET_TEXT_RE.search(verification)):
        raise SnapshotError("snapshot operator verification is invalid")
    commits = manifest.get("source_commits")
    if not isinstance(commits, dict) or set(commits) != {
            "resume_repository_head", "variant_source", "master_source"}:
        raise SnapshotError("snapshot source commit shape is invalid")
    for commit in commits.values():
        if (not isinstance(commit, dict) or set(commit) != {"status", "sha"}
                or commit.get("status") not in ("verified", "unknown")
                or (commit.get("status") == "verified"
                    and (not isinstance(commit.get("sha"), str)
                         or not COMMIT_RE.fullmatch(commit["sha"])))
                or (commit.get("status") == "unknown" and commit.get("sha") is not None)):
            raise SnapshotError("snapshot source commit data is invalid")
    confirmation = manifest.get("confirmation")
    if (not isinstance(confirmation, dict)
            or set(confirmation) != {"submitted_at", "evidence"}):
        raise SnapshotError("snapshot confirmation shape is invalid")
    if confirmation["submitted_at"] is None:
        if confirmation["evidence"] is not None:
            raise SnapshotError("snapshot confirmation data is incomplete")
    else:
        try:
            datetime.strptime(confirmation["submitted_at"], "%Y-%m-%d")
        except (TypeError, ValueError):
            raise SnapshotError("snapshot submission date is invalid")
        if (not isinstance(confirmation["evidence"], str)
                or len(confirmation["evidence"]) < 12
                or SECRET_TEXT_RE.search(confirmation["evidence"])):
            raise SnapshotError("snapshot confirmation evidence is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"resume_pdf", "answers_json"}:
        raise SnapshotError("snapshot artifact shape is invalid")
    for key, folder, suffix in (("resume_pdf", "pdf", ".pdf"),
                                ("answers_json", "answers", ".json")):
        artifact = artifacts[key]
        if (not isinstance(artifact, dict) or set(artifact) != {"sha256", "bytes", "path"}
                or not isinstance(artifact.get("sha256"), str)
                or not SHA256_RE.fullmatch(artifact["sha256"])
                or type(artifact.get("bytes")) is not int or artifact["bytes"] < 0
                or artifact.get("path") != folder + "/" + artifact["sha256"] + suffix):
            raise SnapshotError("snapshot artifact metadata is invalid")
        blob = _read_regular_file(os.path.join(root, folder,
                                               artifact["sha256"] + suffix),
                                  "retained " + key)
        if len(blob) != artifact["bytes"] or sha256(blob) != artifact["sha256"]:
            raise SnapshotError("retained snapshot bytes do not match the manifest")
        if key == "resume_pdf" and not blob.startswith(b"%PDF-"):
            raise SnapshotError("retained resume bytes are not a PDF")
        if key == "answers_json":
            _check_answer_bytes(blob)
    return manifest


def verify_snapshot(snapshot_root, manifest_id, canonical_application_id, attempt_id,
                    pdf_file=None, answers_file=None):
    """Verify manifest, retained bytes, identity binding, and selected bytes."""
    root = _private_dir(snapshot_root)
    manifest = _read_manifest(root, manifest_id)
    if (manifest["canonical_application_id"] != canonical_application_id
            or manifest["attempt_id"] != attempt_id):
        raise SnapshotError("prepared snapshot belongs to another application or attempt")
    if (pdf_file is None) != (answers_file is None):
        raise SnapshotError("both current material files are required")
    if pdf_file is not None:
        pdf, answers = _read_materials(pdf_file, answers_file)
        current = {"resume_pdf": pdf, "answers_json": answers}
        for key, data in current.items():
            artifact = manifest["artifacts"][key]
            if len(data) != artifact["bytes"] or sha256(data) != artifact["sha256"]:
                raise SnapshotError("selected PDF or answers changed; prepare a new snapshot before Submit")
    return manifest


def finalize_snapshot(snapshot_root, manifest_id, canonical_application_id,
                      attempt_id, submitted_at, confirmation_evidence):
    """Publish a new immutable confirmation manifest after queue confirmation."""
    manifest = verify_snapshot(snapshot_root, manifest_id, canonical_application_id,
                               attempt_id)
    if manifest["confirmation"]["submitted_at"] is not None:
        if (manifest["confirmation"]["submitted_at"] != submitted_at
                or manifest["confirmation"]["evidence"] != confirmation_evidence):
            raise SnapshotError("submission confirmation is already finalized")
        return manifest_id
    try:
        datetime.strptime(str(submitted_at), "%Y-%m-%d")
    except (TypeError, ValueError):
        raise SnapshotError("submission date is invalid")
    evidence = validate_confirmation_evidence(confirmation_evidence)
    manifest["confirmation"] = {"submitted_at": submitted_at, "evidence": evidence}
    return _store_manifest(_private_dir(snapshot_root), manifest)
