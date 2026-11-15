#!/usr/bin/env python3
"""Bounded, read-only IMAP ingestion into the private recruitment evidence ledger.

Offline imports use normalized JSON and never contact a mailbox. Live access is
optional and receives its resolved credential only on stdin from the managed
Node adapter. This module never changes tracker stages or sends notifications.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time

import recruitment_evidence as evidence
from config import default_repo_dir, default_runtime_path, path_setting


MAX_FOLDERS = 20
MAX_MESSAGES = 500
MAX_SECONDS = 120
MAX_HEADER_BYTES = 32768
MAX_BODY_BYTES = 262144
MAX_SEARCH_UIDS = 5000
MAX_BATCH = 25
LOCK_WAIT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.05
SOURCE = "imap_readonly"
STATE_SCHEMA = 1


def _bounded_int(value, low, high, label):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid " + label)
    if value < low or value > high:
        raise ValueError("invalid " + label)
    return value


def _safe_name(value, label, maximum=120):
    value = str(value or "")
    if (not value or len(value) > maximum or "\0" in value
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError("invalid " + label)
    return value


def validate_host(value):
    host = _safe_name(value, "host", 253).strip().rstrip(".")
    if not host or "/" in host or "\\" in host or "@" in host:
        raise ValueError("invalid host")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid host")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", ascii_host):
        raise ValueError("invalid host")
    if any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
           for part in ascii_host.split(".")):
        raise ValueError("invalid host")
    return ascii_host


def validate_account(value):
    account = _safe_name(value, "account", 120)
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]+", account):
        raise ValueError("invalid account")
    return account


def validate_email(value, label="account address"):
    value = _safe_name(value, label, 254).strip()
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value):
        raise ValueError("invalid " + label)
    return value.casefold()


def _window(mode, since=None, until=None, repair_name=None):
    if mode not in ("initial", "backfill", "repair"):
        raise ValueError("invalid scan mode")
    if mode == "backfill" and not since:
        raise ValueError("backfill requires --since")
    if mode == "repair" and not repair_name:
        raise ValueError("repair requires --repair-name")
    if mode != "repair" and repair_name:
        raise ValueError("repair name is only valid in repair mode")
    start = date.fromisoformat(since).isoformat() if since else ""
    end = date.fromisoformat(until).isoformat() if until else ""
    if start and end and end < start:
        raise ValueError("invalid scan window")
    name = _safe_name(repair_name, "repair name") if repair_name else ""
    payload = json.dumps([mode, start, end, name], separators=(",", ":"))
    return {"mode": mode, "since": start, "until": end,
            "repair_name": name, "id": hashlib.sha256(payload.encode()).hexdigest()}


def cursor_key(account, folder, window):
    scope = json.dumps([account, folder, window["mode"], window["id"]],
                       ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def empty_state():
    return {"schema": STATE_SCHEMA, "scopes": {}}


def validate_state(state):
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise ValueError("invalid cursor state")
    scopes = state.get("scopes")
    if not isinstance(scopes, dict) or len(scopes) > 1000:
        raise ValueError("invalid cursor state")
    for key, cursor in scopes.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(key)) or not isinstance(cursor, dict):
            raise ValueError("invalid cursor state")
        if (not isinstance(cursor.get("account"), str)
                or not isinstance(cursor.get("folder"), str)
                or cursor.get("mode") not in ("initial", "backfill", "repair")
                or not isinstance(cursor.get("since"), str)
                or not isinstance(cursor.get("until"), str)
                or not isinstance(cursor.get("repair_name"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(cursor.get("window_id") or ""))
                or not isinstance(cursor.get("uidvalidity"), str)
                or not cursor["uidvalidity"].isdigit()
                or not isinstance(cursor.get("uid"), int) or cursor["uid"] < 0):
            raise ValueError("invalid cursor state")
        expected_window = _window(cursor["mode"], cursor["since"] or None,
                                  cursor["until"] or None,
                                  cursor["repair_name"] or None)
        if expected_window["id"] != cursor["window_id"]:
            raise ValueError("invalid cursor state")
        expected = json.dumps([cursor["account"], cursor["folder"], cursor["mode"],
                               cursor["window_id"]], ensure_ascii=True,
                              separators=(",", ":"))
        if hashlib.sha256(expected.encode("utf-8")).hexdigest() != key:
            raise ValueError("invalid cursor state")
    return state


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix="." + path.name + ".",
                                             suffix=".tmp", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def state_lock(path, timeout=LOCK_WAIT_SECONDS):
    lock_path = str(path) + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(Path(lock_path).parent, 0o700)
    with open(lock_path, "a+b") as lock:
        if os.name != "nt":
            os.chmod(lock_path, 0o600)
        deadline = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0)
                    if lock.read(1) == b"":
                        lock.seek(0)
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock busy")
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_state(path):
    path = Path(path)
    if not path.exists():
        return empty_state()
    try:
        return validate_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid cursor state")


def validate_ledger(path):
    path = Path(path)
    if not path.exists():
        return {"schema": 1, "events": [], "coverage": {}, "review_required": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("events"), list):
            raise ValueError()
        if any(not isinstance(event, dict) or not isinstance(event.get("event_id"), str)
               or not event["event_id"] for event in doc["events"]):
            raise ValueError()
        if not isinstance(doc.get("coverage", {}), (dict, list)):
            raise ValueError()
        evidence.effective_events(doc)
        return doc
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("invalid evidence ledger")


def load_tracker(path):
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise ValueError("tracker unavailable")
    import summary
    rows = summary.parse_yaml_list(raw)
    content = [line.strip() for line in raw.splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    if not content or (not rows and "".join(content) != "[]"):
        raise ValueError("invalid tracker")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid tracker")
    return rows


def _header_value(message, name):
    value = message.get(name)
    if value is None:
        return ""
    value = str(value).replace("\r", " ").replace("\n", " ").strip()
    return value[:2048]


def _stable_message_id(headers, raw_message, account):
    msgid = _header_value(headers, "Message-ID")
    if msgid and len(msgid) <= 998 and "\0" not in msgid:
        return msgid.casefold()
    # Weak IDs deduplicate identical folder copies but cannot verify an outcome.
    return "weak:" + hashlib.sha256(account.encode() + b"|" + raw_message).hexdigest()


def _extract_text(raw):
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    plain, html_parts = [], []
    for part in parsed.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_filename():
            continue
        content_type = part.get_content_type().lower()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, KeyError, ValueError):
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                content = payload.decode(charset, "replace")
            except LookupError:
                content = payload.decode("utf-8", "replace")
        if isinstance(content, str) and content:
            (plain if content_type == "text/plain" else html_parts).append(content)
    return "\n".join(plain or html_parts)


def normalize_message(raw_headers, raw_body, *, size, account, folder,
                      uidvalidity, uid, account_address, max_body_bytes):
    headers = BytesParser(policy=policy.default).parsebytes(raw_headers)
    header_ok = len(raw_headers) <= MAX_HEADER_BYTES
    actual_size = size if isinstance(size, int) and size >= 0 else None
    body_complete = (header_ok and actual_size is not None
                     and len(raw_body) == actual_size
                     and actual_size <= max_body_bytes)
    body = _extract_text(raw_body) if body_complete else ""
    sender = _header_value(headers, "From")
    addresses = [address.casefold() for _, address in getaddresses([sender]) if address]
    direction = "outbound" if account_address and account_address in addresses else "inbound"
    occurred_at = _header_value(headers, "Date")
    message_id = _stable_message_id(headers, raw_headers + raw_body, account)
    limitations = []
    if not header_ok:
        limitations.append("header_size_limit")
    if actual_size is None:
        limitations.append("message_size_unavailable")
    elif actual_size > max_body_bytes:
        limitations.append("body_size_limit")
    elif not body_complete:
        limitations.append("incomplete_body")
    return {
        "account": account,
        "message_id": message_id,
        "from": sender,
        "subject": _header_value(headers, "Subject"),
        "date": occurred_at,
        "occurred_at": occurred_at,
        "body": body,
        "body_complete": body_complete,
        "direction": direction,
        "source": SOURCE,
        "provider_identity": {"account": account,
                              "rfc822_message_id": message_id},
        "fetch_limitations": limitations,
    }


def _message_in_window(message, window):
    if not window["since"] and not window["until"]:
        return True
    exact = evidence.exact_date(message.get("date"))
    if not exact:
        return True  # retain as review evidence; the exact date is unavailable
    day = date.fromisoformat(exact[:10])
    return ((not window["since"] or day >= date.fromisoformat(window["since"]))
            and (not window["until"] or day <= date.fromisoformat(window["until"])))


def _coverage_row(account, folder, window, uidvalidity, counts, *, status,
                  transport_complete, reset=False, interrupted=False):
    return {"source": SOURCE, "account": account, "folder": folder,
            "mode": window["mode"], "window_id": window["id"],
            "since": window["since"], "until": window["until"],
            "repair_name": window["repair_name"],
            "uidvalidity": uidvalidity, "transport_complete": transport_complete,
            "status": status, "uid_reset": reset, "interrupted": interrupted,
            **counts}


def _commit_batch(messages, tracker_rows, ledger_path, state, scope_key,
                  cursor, progress_uid, persist, coverage, state_path):
    incoming = evidence.reconcile(messages, tracker_rows, coverage=coverage)
    # Evidence and coverage become durable before progress can skip the UIDs.
    persist(ledger_path, incoming)
    cursor.update(uid=progress_uid)
    state["scopes"][scope_key] = dict(cursor)
    _atomic_json(state_path, state)


def run_scan(transport, *, account, account_address, folders, tracker_rows,
             ledger_path, state_path, state, mode="initial", since=None,
             until=None, repair_name=None, max_messages=100, max_seconds=30,
             max_body_bytes=MAX_BODY_BYTES, max_header_bytes=MAX_HEADER_BYTES,
             max_folders=MAX_FOLDERS, persist=None, clock=None):
    """Run a deterministic bounded scan against an injected transport.

    ``transport`` implements list/select/search/header/body methods. ``persist``
    defaults to the issue-5 evidence writer and is injectable for fault tests.
    """
    account = validate_account(account)
    window = _window(mode, since, until, repair_name)
    max_messages = _bounded_int(max_messages, 1, MAX_MESSAGES, "message limit")
    max_seconds = _bounded_int(max_seconds, 1, MAX_SECONDS, "time limit")
    max_body_bytes = _bounded_int(max_body_bytes, 1024, MAX_BODY_BYTES, "body limit")
    max_header_bytes = _bounded_int(max_header_bytes, 1024, MAX_HEADER_BYTES, "header limit")
    max_folders = _bounded_int(max_folders, 1, MAX_FOLDERS, "folder limit")
    validate_state(state)
    persist = persist or evidence.save_ledger
    clock = clock or (lambda: datetime.now(timezone.utc))
    folder_names = []
    for value in folders:
        name = _safe_name(value, "folder", 255)
        if name not in folder_names:
            folder_names.append(name)
    excluded_folders = max(0, len(folder_names) - max_folders)
    excluded_folder_names = folder_names[max_folders:]
    folder_names = folder_names[:max_folders]
    started = time.monotonic()
    scanned = 0
    records = []
    interrupted = False
    total_counts = {"candidate_messages": 0, "candidate_exclusions": 0,
                    "oversized_bodies": 0, "incomplete_bodies": 0,
                    "header_limit_exclusions": 0, "unreadable_folders": 0}
    folder_report = []
    for folder in folder_names:
        counts = {"candidate_messages": 0, "candidate_exclusions": 0,
                  "oversized_bodies": 0, "incomplete_bodies": 0,
                  "header_limit_exclusions": 0}
        scope_key = cursor_key(account, folder, window)
        old = state["scopes"].get(scope_key, {})
        try:
            selection = transport.select(folder)
            if not isinstance(selection, dict) or not selection.get("ok"):
                raise OSError("folder unavailable")
            uidvalidity = str(selection.get("uidvalidity") or "")
            if not uidvalidity.isdigit():
                raise OSError("uidvalidity unavailable")
            reset = bool(old and old.get("uidvalidity") != uidvalidity)
            cursor_uid = 0 if reset or not old else old["uid"]
            uids = transport.search(folder, cursor_uid)
            if not isinstance(uids, (list, tuple)):
                raise OSError("search unavailable")
            def parse_uid(uid):
                if isinstance(uid, bytes):
                    uid = uid.decode("ascii", "ignore")
                text = str(uid)
                return int(text) if text.isdigit() and int(text) > 0 else None
            clean_uids = sorted({parsed for uid in uids
                                 if (parsed := parse_uid(uid)) is not None})
            todo = [uid for uid in clean_uids if uid > cursor_uid]
            candidate_truncated = len(todo) > MAX_SEARCH_UIDS
            todo = todo[:MAX_SEARCH_UIDS]
            processed = 0
            progress_uid = cursor_uid
            batch = []
            folder_interrupted = False

            def commit(progress):
                coverage_rows = records + [_coverage_row(
                    account, folder, window, uidvalidity, counts,
                    status="scanning", transport_complete=False, reset=reset,
                    interrupted=folder_interrupted)]
                coverage = {"complete": False, "transport_complete": False,
                            "source": SOURCE, "account": account,
                            "last_scan_at": clock().isoformat(), "sources": coverage_rows,
                            "limitations": ["Caught-up transport does not prove complete callback recall"]}
                _commit_batch(batch, tracker_rows, ledger_path, state, scope_key,
                              {"account": account, "folder": folder, "mode": mode,
                               "window_id": window["id"], "since": window["since"],
                               "until": window["until"], "repair_name": window["repair_name"],
                               "uidvalidity": uidvalidity,
                               "uid": cursor_uid}, progress, persist, coverage, state_path)
                batch.clear()

            if reset:
                # Record the reset and the new UIDVALIDITY even for an empty mailbox.
                progress_uid = 0
            for uid in todo:
                if scanned >= max_messages or time.monotonic() - started >= max_seconds:
                    interrupted = folder_interrupted = True
                    break
                try:
                    raw_headers, size = transport.headers(uid)
                    if not isinstance(raw_headers, bytes):
                        raise OSError("header fetch failed")
                    if len(raw_headers) > max_header_bytes:
                        counts["header_limit_exclusions"] += 1
                        counts["candidate_exclusions"] += 1
                        processed += 1
                        progress_uid = uid
                    else:
                        raw_body = b""
                        if isinstance(size, int) and 0 <= size <= max_body_bytes:
                            raw_body = transport.body(uid, max_body_bytes + 1)
                            if not isinstance(raw_body, bytes):
                                raise OSError("body fetch failed")
                        message = normalize_message(
                            raw_headers, raw_body, size=size, account=account,
                            folder=folder, uidvalidity=uidvalidity, uid=uid,
                            account_address=account_address,
                            max_body_bytes=max_body_bytes)
                        # MIME is parsed only for a complete body within the cap.
                        message["body_complete"] = bool(
                            message["body_complete"] and len(raw_headers) <= max_header_bytes)
                        if isinstance(size, int) and size > max_body_bytes:
                            counts["oversized_bodies"] += 1
                        elif not message["body_complete"]:
                            counts["incomplete_bodies"] += 1
                        if not _message_in_window(message, window):
                            counts["candidate_exclusions"] += 1
                        else:
                            batch.append(message)
                            counts["candidate_messages"] += 1
                        processed += 1
                        progress_uid = uid
                except Exception:
                    # Do not commit the failing UID. Earlier batch evidence may
                    # be committed and safely replayed if its cursor write fails.
                    interrupted = folder_interrupted = True
                    break
                scanned += 1
                if processed >= MAX_BATCH:
                    commit(progress_uid)
                    cursor_uid = progress_uid
                    processed = 0

            if processed or reset or (not todo and not old):
                commit(progress_uid)
                cursor_uid = progress_uid
            if candidate_truncated:
                counts["candidate_exclusions"] += 1
                folder_interrupted = True
                interrupted = True
            folder_status = "interrupted" if folder_interrupted else "scanned"
            if folder_interrupted:
                interrupted = True
            row = _coverage_row(account, folder, window, uidvalidity, counts,
                                status=folder_status,
                                transport_complete=not folder_interrupted and not candidate_truncated,
                                reset=reset, interrupted=folder_interrupted)
            row["last_uid"] = cursor_uid
            row["candidate_list_truncated"] = candidate_truncated
            records.append(row)
            folder_report.append(row)
        except Exception:
            interrupted = True
            total_counts["unreadable_folders"] += 1
            row = _coverage_row(account, folder, window, "", counts,
                                status="unreadable", transport_complete=False,
                                interrupted=True)
            row["reason"] = "folder_unreadable"
            folder_report.append(row)

    for folder in excluded_folder_names:
        row = _coverage_row(account, folder, window, "", {}, status="skipped",
                            transport_complete=False, interrupted=True)
        row["reason"] = "folder_limit"
        folder_report.append(row)

    finished = not interrupted and excluded_folders == 0
    # Always write coverage, including empty scans and folders that could not be read.
    coverage = {"complete": False, "transport_complete": finished,
                "source": SOURCE, "account": account,
                "last_scan_at": clock().isoformat(), "scanned_folders": len(records),
                "sources": records + [row for row in folder_report if row not in records],
                "limitations": ["Caught-up transport does not prove complete callback recall"]}
    try:
        persist(ledger_path, evidence.reconcile([], tracker_rows, coverage=coverage))
    except Exception:
        # The final health write is also the durability boundary. No cursor is
        # advanced here; all prior progress was committed with its own ledger.
        raise
    return {"scanned": scanned, "candidate_messages": sum(
                item.get("candidate_messages", 0) for item in folder_report),
            "candidate_exclusions": sum(item.get("candidate_exclusions", 0)
                                         for item in folder_report),
            "oversized_bodies": sum(item.get("oversized_bodies", 0) for item in folder_report),
            "incomplete_bodies": sum(item.get("incomplete_bodies", 0) for item in folder_report),
            "unreadable_folders": total_counts["unreadable_folders"],
            "uid_resets": sum(bool(item.get("uid_reset")) for item in folder_report),
            "interrupted": interrupted or excluded_folders > 0,
            "excluded_folders": excluded_folders,
            "folder_limit_reached": excluded_folders > 0,
            "transport_complete": finished,
            "folder_results": folder_report,
            "coverage": coverage}


class IMAPTransport:
    """Small read-only wrapper; all message data is fetched with BODY.PEEK."""
    def __init__(self, client):
        self.client = client

    def select(self, folder):
        status, _ = self.client.select(folder, readonly=True)
        response = self.client.response("UIDVALIDITY")
        values = response[1] if isinstance(response, tuple) and len(response) > 1 else []
        uidvalidity = next((item.decode("ascii", "ignore") if isinstance(item, bytes) else str(item)
                            for item in values if str(item).strip()), "")
        return {"ok": str(status).upper() == "OK", "uidvalidity": uidvalidity}

    def search(self, folder, after_uid=0):
        # A bounded UID range lets a later run continue beyond a full page.
        status, data = self.client.uid("SEARCH", None, "UID",
                                       "%d:*" % (int(after_uid) + 1))
        if str(status).upper() != "OK" or not data:
            raise OSError("search failed")
        raw = data[0] or b""
        return [match.group(0) for index, match in enumerate(re.finditer(rb"\d+", raw))
                if index <= MAX_SEARCH_UIDS]

    def _fetch(self, uid, query):
        status, data = self.client.uid("FETCH", str(uid), query)
        if str(status).upper() != "OK" or not data:
            raise OSError("fetch failed")
        literals = [part for part in data if isinstance(part, tuple) and len(part) > 1
                    and isinstance(part[1], bytes)]
        if not literals:
            raise OSError("fetch body unavailable")
        meta = literals[0][0]
        return meta, literals[0][1]

    def headers(self, uid):
        meta, raw = self._fetch(uid, "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]<0.%d>)" % (MAX_HEADER_BYTES + 1))
        match = re.search(rb"RFC822\.SIZE\s+(\d+)", meta, re.I)
        return raw, int(match.group(1)) if match else None

    def body(self, uid, limit):
        _, raw = self._fetch(uid, "(UID BODY.PEEK[]<0.%d>)" % limit)
        return raw[:limit]


def _folder_list(value):
    folders = [part.strip() for part in str(value or "INBOX").split(",") if part.strip()]
    if not folders or len(folders) > 100:
        raise ValueError("invalid folder list")
    return [_safe_name(name, "folder", 255) for name in folders]


def _read_credential_stdin():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, OSError, UnicodeError):
        raise ValueError("managed credential input unavailable")
    if not isinstance(value, dict):
        raise ValueError("managed credential input unavailable")
    host = validate_host(value.get("host"))
    account = validate_account(value.get("account"))
    user = validate_email(value.get("user"), "mailbox username")
    password = value.get("password")
    if (not isinstance(password, str) or not password or len(password) > 16384
            or "\0" in password):
        raise ValueError("managed credential input unavailable")
    port = _bounded_int(value.get("port", 993), 1, 65535, "port")
    return {"host": host, "account": account, "user": user,
            "password": password, "port": port}


def _managed_scan(args):
    credential = _read_credential_stdin()
    tracker_path = Path(path_setting("JOBSEARCH_TRACKER", Path(default_repo_dir()) / "applications.yaml"))
    ledger_path = Path(path_setting("JOBSEARCH_RECRUITMENT_LEDGER",
                                    default_runtime_path("recruitment_events.json")))
    state_path = Path(path_setting("JOBSEARCH_INGEST_STATE",
                                   default_runtime_path("email_ingest_state.json")))
    folders = _folder_list(os.environ.get("JOBSEARCH_EMAIL_FOLDERS", "INBOX"))
    address = validate_email(os.environ["JOBSEARCH_EMAIL_ADDRESS"])
    tracker_rows = load_tracker(tracker_path)
    validate_ledger(ledger_path)
    window = _window(args.mode, args.since, args.until, args.repair_name)
    # Validate every limit and value before opening a socket.
    _bounded_int(args.max_messages, 1, MAX_MESSAGES, "message limit")
    _bounded_int(args.max_seconds, 1, MAX_SECONDS, "time limit")
    _bounded_int(args.max_body_bytes, 1024, MAX_BODY_BYTES, "body limit")
    with state_lock(state_path):
        state = load_state(state_path)
        server = imaplib.IMAP4_SSL(credential["host"], credential["port"],
                                   ssl_context=ssl.create_default_context(), timeout=15)
        try:
            server.login(credential["user"], credential["password"])
            transport = IMAPTransport(server)
            result = run_scan(transport, account=credential["account"],
                              account_address=address, folders=folders,
                              tracker_rows=tracker_rows, ledger_path=ledger_path,
                              state_path=state_path, state=state, mode=window["mode"],
                              since=window["since"] or None, until=window["until"] or None,
                              repair_name=window["repair_name"] or None,
                              max_messages=args.max_messages, max_seconds=args.max_seconds,
                              max_body_bytes=args.max_body_bytes)
        finally:
            try:
                server.logout()
            except Exception:
                pass
    print(json.dumps({key: result[key] for key in (
        "scanned", "candidate_messages", "candidate_exclusions", "oversized_bodies",
        "incomplete_bodies", "unreadable_folders", "uid_resets", "interrupted",
        "transport_complete", "excluded_folders", "folder_limit_reached")}))
    return 0


def _offline_import(args):
    tracker_path = Path(args.tracker)
    ledger_path = Path(args.ledger)
    messages_path = Path(args.messages)
    tracker_rows = load_tracker(tracker_path)
    validate_ledger(ledger_path)
    try:
        document = json.loads(messages_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise ValueError("normalized messages unavailable")
    messages = document.get("messages", []) if isinstance(document, dict) else document
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise ValueError("invalid normalized messages")
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("invalid normalized messages")
        body = message.get("body", message.get("text", ""))
        if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("normalized body limit exceeded")
        if not isinstance(message.get("body_complete", False), bool):
            raise ValueError("invalid normalized messages")
    coverage = (document.get("coverage", {}) if isinstance(document, dict) else {})
    if not isinstance(coverage, dict):
        raise ValueError("invalid normalized coverage")
    result = evidence.save_ledger(ledger_path,
                                  evidence.reconcile(messages, tracker_rows,
                                                     coverage=dict(coverage, complete=False)))
    print(json.dumps({"imported": len(messages),
                      "review_required": len(result.get("review_required", [])),
                      "transport_complete": False}))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("import", help="import normalized messages offline; no mailbox access")
    offline.add_argument("--messages", required=True)
    offline.add_argument("--tracker", default=path_setting(
        "JOBSEARCH_TRACKER", Path(default_repo_dir()) / "applications.yaml"))
    offline.add_argument("--ledger", default=path_setting(
        "JOBSEARCH_RECRUITMENT_LEDGER", default_runtime_path("recruitment_events.json")))
    live = sub.add_parser("managed-scan", help="read-only scan; invoke through the managed adapter")
    live.add_argument("--credential-stdin", action="store_true", required=True)
    live.add_argument("--mode", choices=("initial", "backfill", "repair"), default="initial")
    live.add_argument("--since")
    live.add_argument("--until")
    live.add_argument("--repair-name")
    live.add_argument("--max-messages", type=int, default=100)
    live.add_argument("--max-seconds", type=int, default=30)
    live.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        return _offline_import(args) if args.command == "import" else _managed_scan(args)
    except Exception:
        # Avoid echoing provider/SDK exceptions: some libraries include login
        # responses or configuration values in their diagnostic text.
        print("Email ingestion failed; no sensitive diagnostic was emitted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
