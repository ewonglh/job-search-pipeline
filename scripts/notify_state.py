#!/usr/bin/env python3
"""Bounded, private runtime state for optional notification receipt handling."""
import json
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager

from config import default_runtime_path, path_setting


SCHEMA_VERSION = 2
RETENTION_DAYS = 30
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_PENDING_BATCHES = 100
MAX_MESSAGE_BYTES = 8192
MAX_DECISIONS = 2048
MAX_ACKNOWLEDGMENTS = 1000
MAX_DISCOVERED_RUNS = 2000
STATE_PATH = path_setting("JOBSEARCH_NOTIFY_STATE",
                          default_runtime_path("notify_state.json"))
_BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class StateError(ValueError):
    """State is malformed, unsupported, or cannot be updated safely."""


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "retention_floor": None,
        "discovery": {"last_scan_at": None, "latest_run_at_ms": 0,
                      "run_ids": []},
        "decisions": {},
        "pending_batches": {},
        "acknowledgments": {},
        "legacy_quarantine": [],
    }


def parse_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Python 3.9's fromisoformat may reject the compact offset emitted by
        # older runtime state, while strptime accepts that documented form.
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def _iso(value):
    try:
        return parse_timestamp(value).tzinfo is not None
    except ValueError:
        return False


def _validate(state):
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise StateError("unsupported notification state schema")
    required = {"schema_version", "retention_floor", "discovery", "decisions", "pending_batches",
                "acknowledgments", "legacy_quarantine"}
    if set(state) != required:
        raise StateError("invalid notification state fields")
    if state["retention_floor"] is not None and not _iso(state["retention_floor"]):
        raise StateError("invalid notification retention floor")
    discovery = state["discovery"]
    if (not isinstance(discovery, dict)
            or set(discovery) != {"last_scan_at", "latest_run_at_ms", "run_ids"}
            or (discovery["last_scan_at"] is not None
                and not _iso(discovery["last_scan_at"]))
            or not isinstance(discovery["latest_run_at_ms"], int)
            or isinstance(discovery["latest_run_at_ms"], bool)
            or discovery["latest_run_at_ms"] < 0
            or not isinstance(discovery["run_ids"], list)
            or len(discovery["run_ids"]) > MAX_DISCOVERED_RUNS
            or any(not isinstance(item, str) or not item for item in discovery["run_ids"])):
        raise StateError("invalid notification discovery state")
    decisions = state["decisions"]
    if not isinstance(decisions, dict) or len(decisions) > MAX_DECISIONS:
        raise StateError("invalid notification decision state")
    for key, item in decisions.items():
        if (not isinstance(key, str) or not _DECISION_ID_RE.fullmatch(key)
                or not isinstance(item, dict)
                or set(item) != {"outcome", "recorded_at", "batch_id",
                                 "message_sha256", "resolution", "resolved_at", "resolution_reason"}
                or item["outcome"] not in ("deliver", "suppressed", "silent")
                or not _iso(item["recorded_at"])):
            raise StateError("invalid notification decision entry")
        bound = item["batch_id"] is not None
        if (item["resolution"] not in (None, "delivered", "dismissed")
                or (item["resolution"] is None and item["resolution_reason"] is not None)
                or (item["resolution"] is not None and not isinstance(item["resolution_reason"], str))
                or (item["resolution"] is None and item["resolved_at"] is not None)
                or (item["resolution"] is not None and not _iso(item["resolved_at"]))
                or (bound and (not isinstance(item["batch_id"], str)
                    or not _BATCH_ID_RE.fullmatch(item["batch_id"])))
                or (bound and not re.fullmatch(r"[0-9a-f]{64}", str(item["message_sha256"])))
                or (not bound and (item["message_sha256"] is not None
                                   or item["resolution"] is not None))
                or (item["outcome"] != "deliver" and bound)):
            raise StateError("invalid notification decision binding")
    pending = state["pending_batches"]
    if not isinstance(pending, dict) or len(pending) > MAX_PENDING_BATCHES:
        raise StateError("invalid pending notification batches")
    for key, batch in pending.items():
        if (not isinstance(key, str) or not _BATCH_ID_RE.fullmatch(key)
                or not isinstance(batch, dict)
                or set(batch) != {"message", "created_at", "decision_id",
                                  "native_run_id", "review_reason"}
                or not isinstance(batch["message"], str)
                or not batch["message"].strip()
                or len(batch["message"].encode("utf-8")) > MAX_MESSAGE_BYTES
                or not _iso(batch["created_at"])
                or not isinstance(batch["decision_id"], str)
                or not _DECISION_ID_RE.fullmatch(batch["decision_id"])
                or (batch["native_run_id"] is not None
                    and not isinstance(batch["native_run_id"], str))
                or (batch["review_reason"] is not None
                    and not isinstance(batch["review_reason"], str))):
            raise StateError("invalid pending notification batch")
        decision = decisions.get(batch["decision_id"])
        if not decision or decision["outcome"] != "deliver":
            raise StateError("pending notification batch has no deliver decision")
        if (decision["batch_id"] != key or decision["resolution"] is not None
                or decision["message_sha256"] != hashlib.sha256(
                    batch["message"].encode("utf-8")).hexdigest()):
            raise StateError("pending batch does not match its decision binding")
    acknowledgments = state["acknowledgments"]
    if not isinstance(acknowledgments, dict) or len(acknowledgments) > MAX_ACKNOWLEDGMENTS:
        raise StateError("invalid notification acknowledgment state")
    for key, item in acknowledgments.items():
        if (not isinstance(key, str) or not _BATCH_ID_RE.fullmatch(key)
                or not isinstance(item, dict)
                or set(item) != {"acknowledged_at", "native_run_id", "message_sha256"}
                or not _iso(item["acknowledged_at"])
                or not isinstance(item["native_run_id"], str)
                or not item["native_run_id"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(item["message_sha256"]))):
            raise StateError("invalid notification acknowledgment entry")
    quarantine = state["legacy_quarantine"]
    bindings = [d["batch_id"] for d in decisions.values() if d["batch_id"]]
    if len(bindings) != len(set(bindings)):
        raise StateError("multiple decisions bind the same batch")
    for decision in decisions.values():
        bound = decision["batch_id"]
        if bound is None:
            continue
        if (decision["resolution"] == "delivered" and bound not in acknowledgments
                or decision["resolution"] == "dismissed" and bound in acknowledgments
                or decision["resolution"] is None and bound not in pending):
            raise StateError("decision resolution does not match batch state")
    if set(acknowledgments) - set(bindings):
        raise StateError("acknowledgment has no decision binding")
    if not isinstance(quarantine, list) or len(quarantine) > 16:
        raise StateError("invalid legacy notification quarantine")
    for item in quarantine:
        if (not isinstance(item, dict)
                or set(item) != {"status", "cursor_count", "reason"}
                or item["status"] != "quarantined"
                or not isinstance(item["cursor_count"], int)
                or item["cursor_count"] < 0
                or item["reason"] != "legacy positions are not safe to resume"):
            raise StateError("invalid legacy notification quarantine entry")
    return state


def _legacy_state(raw):
    """Quarantine old positions and retain old pending batches for review only."""
    if not isinstance(raw, dict):
        return None
    is_legacy = ("cursors" in raw or "cursor" in raw or "batches" in raw
                 or raw.get("version") == 2)
    if not is_legacy:
        return None
    cursors = raw.get("cursors")
    cursor = raw.get("cursor")
    positions = 0
    if isinstance(cursors, dict):
        positions += len(cursors)
    elif cursors is not None:
        positions += 1
    if cursor is not None:
        positions += 1
    state = empty_state()
    state["legacy_quarantine"].append({
        "status": "quarantined",
        "cursor_count": positions,
        "reason": "legacy positions are not safe to resume",
    })
    batches = raw.get("batches", {})
    if not isinstance(batches, dict) or len(batches) > MAX_PENDING_BATCHES:
        raise StateError("legacy pending batches exceed the supported migration bound")
    for batch_id, old in batches.items():
        if (not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id)
                or not isinstance(old, dict)
                or not isinstance(old.get("text"), str)
                or not old["text"].strip()
                or len(old["text"].encode("utf-8")) > MAX_MESSAGE_BYTES
                or not _iso(old.get("created_at"))):
            raise StateError("legacy pending batch cannot be migrated safely")
        decision_id = "legacy:" + batch_id
        state["decisions"][decision_id] = {
            "outcome": "deliver", "recorded_at": old["created_at"],
            "batch_id": batch_id,
            "message_sha256": hashlib.sha256(old["text"].encode("utf-8")).hexdigest(),
            "resolution": None, "resolved_at": None, "resolution_reason": None}
        state["pending_batches"][batch_id] = {
            "message": old["text"],
            "created_at": old["created_at"],
            "decision_id": decision_id,
            "native_run_id": None,
            "review_reason": "legacy_batch_requires_review",
        }
    return state


def load_state(path=STATE_PATH):
    """Read current state; legacy positions are quarantined, never resumed."""
    path = os.fspath(path)
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return empty_state()
    if size > MAX_STATE_BYTES:
        raise StateError("notification state exceeds its size bound")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError) as exc:
        raise StateError("notification state cannot be read") from exc
    if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
        return _validate(raw)
    if isinstance(raw, dict) and "schema_version" in raw:
        raise StateError("unsupported notification state schema")
    legacy = _legacy_state(raw)
    if legacy is not None:
        return legacy
    raise StateError("unsupported notification state schema")


def _write_atomic(path, state):
    _validate(state)
    encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise StateError("notification state exceeds its size bound")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".notify-state-", dir=parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


@contextmanager
def _writer_lock(path):
    """Use an OS lock so a process crash cannot strand a permanent lock."""
    path = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise StateError("notification state is locked by another writer") from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StateError("notification state is locked by another writer") from exc
        yield
    finally:
        if os.name == "nt":
            try:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def update_state(path, mutator):
    """Lock one read/modify/write transaction; dry-run readers never lock."""
    path = os.path.abspath(os.fspath(path))
    with _writer_lock(path):
        state = load_state(path)
        result = mutator(state)
        _write_atomic(path, state)
        return result


def _timestamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _decision_id(value):
    if not isinstance(value, str) or not _DECISION_ID_RE.fullmatch(value):
        raise StateError("decision id must be a short stable identifier")
    return value


def record_decision(state, decision_id, outcome, recorded_at=None):
    """Record the upstream gate outcome without making any model call."""
    decision_id = _decision_id(decision_id)
    if outcome not in ("deliver", "suppressed", "silent"):
        raise StateError("decision outcome must be deliver, suppressed, or silent")
    stamp = _timestamp(recorded_at)
    floor = state["retention_floor"]
    if floor is not None and parse_timestamp(stamp) <= parse_timestamp(floor):
        raise StateError("decision predates the retained replay boundary")
    item = {"outcome": outcome, "recorded_at": stamp, "batch_id": None,
            "message_sha256": None, "resolution": None, "resolved_at": None,
            "resolution_reason": None}
    previous = state["decisions"].get(decision_id)
    if previous is not None:
        if (previous["outcome"] != outcome
                or (recorded_at is not None and previous["recorded_at"] != stamp)):
            raise StateError("decision id already has a different outcome or timestamp")
        return False
    if len(state["decisions"]) >= MAX_DECISIONS:
        raise StateError("notification decision history reached its bound")
    state["decisions"][decision_id] = item
    return True


def add_pending_batch(state, decision_id, message, created_at=None, batch_id=None):
    """Persist a bounded message only after its upstream decision is deliver."""
    decision_id = _decision_id(decision_id)
    decision = state["decisions"].get(decision_id)
    if not decision or decision["outcome"] != "deliver":
        raise StateError("a deliver decision is required before staging a batch")
    if not isinstance(message, str) or not message.strip():
        raise StateError("notification message must not be empty")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise StateError("notification message exceeds its size bound")
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    if decision["batch_id"] is not None:
        if decision["message_sha256"] != message_hash:
            raise StateError("decision id already has a different message")
        return decision["batch_id"]
    if len(state["pending_batches"]) >= MAX_PENDING_BATCHES:
        raise StateError("pending notification batch limit reached")
    if batch_id is None:
        import secrets
        batch_id = secrets.token_hex(16)
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise StateError("batch id must be 32 lowercase hexadecimal characters")
    if batch_id in state["pending_batches"] or batch_id in state["acknowledgments"]:
        raise StateError("batch id already exists")
    state["pending_batches"][batch_id] = {
        "message": message,
        "created_at": _timestamp(created_at),
        "decision_id": decision_id,
        "native_run_id": None,
        "review_reason": None,
    }
    decision.update(batch_id=batch_id, message_sha256=message_hash)
    return batch_id


def resolve_batch(state, batch_id, now=None):
    """Close a reviewed notice without claiming that it was delivered."""
    batch = state["pending_batches"].get(batch_id)
    if batch is None or not batch["review_reason"]:
        raise StateError("only a pending notice with a review reason can be dismissed")
    state["decisions"][batch["decision_id"]]["resolution"] = "dismissed"
    state["decisions"][batch["decision_id"]]["resolved_at"] = _timestamp(now)
    state["decisions"][batch["decision_id"]]["resolution_reason"] = batch["review_reason"]
    del state["pending_batches"][batch_id]
    return {"resolved": batch_id, "resolution": "dismissed", "delivery": "unknown"}


def compact_state(state, before, now=None):
    """Remove old terminal detail, retaining a replay boundary for older decisions."""
    cutoff = parse_timestamp(before)
    current = parse_timestamp(_timestamp(now))
    if cutoff.tzinfo is None or cutoff.timestamp() > current.timestamp() - RETENTION_DAYS * 86400:
        raise StateError("compaction must retain at least 30 days")
    floor = state["retention_floor"]
    if floor is not None and cutoff < parse_timestamp(floor):
        raise StateError("retention boundary cannot move backwards")
    removed = 0
    for decision_id, decision in list(state["decisions"].items()):
        if parse_timestamp(decision["recorded_at"]) > cutoff:
            continue
        if decision["resolved_at"] and parse_timestamp(decision["resolved_at"]) > cutoff:
            continue
        batch_id = decision["batch_id"]
        if batch_id in state["pending_batches"]:
            continue
        if decision["outcome"] == "deliver" and decision["resolution"] is None:
            continue
        if batch_id:
            state["acknowledgments"].pop(batch_id, None)
        del state["decisions"][decision_id]
        removed += 1
    state["retention_floor"] = cutoff.isoformat(timespec="seconds")
    return {"compacted": removed, "retention_floor": state["retention_floor"],
            "pending": len(state["pending_batches"])}
