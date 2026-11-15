"""Synthetic bounded ingestion, replay, and failure-recovery checks."""
from email.message import EmailMessage
from email import policy
import contextlib
import io
from pathlib import Path
import json
import tempfile

import email_ingest as ingest
import recruitment_evidence as evidence


TRACKER = [{"company": "Atlas", "role": "Data Intern",
            "link": "https://jobs.example/role?job_id=42", "pr": 7,
            "applied_date": "2026-01-10"}]
ADDRESS = "candidate@example.invalid"


def raw_message(message_id="<m1@example.invalid>", sender="recruiter@atlas.example",
                date="Sun, 01 Feb 2026 10:00:00 +0000",
                body="Atlas invites you to complete the online assessment."):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ADDRESS
    msg["Subject"] = "Atlas Data Intern"
    msg["Date"] = date
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes(policy=policy.SMTP)


class FakeIMAP:
    def __init__(self, folders=None, uidvalidity="11", failures=()):
        self.folders = folders or {"INBOX": {1: raw_message()}}
        self.validities = {folder: uidvalidity for folder in self.folders}
        self.failures = set(failures)
        self.selected = None

    def select(self, folder):
        self.selected = folder
        if folder not in self.folders:
            return {"ok": False}
        return {"ok": True, "uidvalidity": self.validities[folder]}

    def search(self, folder, after_uid=0):
        return sorted(uid for uid in self.folders[folder] if uid > after_uid)

    def headers(self, uid):
        raw = self.folders[self.selected][uid]
        header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        return header, len(raw)

    def body(self, uid, limit):
        if (self.selected, uid) in self.failures:
            raise OSError("synthetic transport failure")
        return self.folders[self.selected][uid][:limit]


def setup(tmp):
    root = Path(tmp)
    return root / "events.json", root / "cursor.json", ingest.empty_state()


def scan(transport, ledger, state_path, state, **kwargs):
    return ingest.run_scan(transport, account="synthetic", account_address=ADDRESS,
                           folders=kwargs.pop("folders", ["INBOX"]),
                           tracker_rows=TRACKER, ledger_path=ledger,
                           state_path=state_path, state=state, clock=lambda:
                           ingest.datetime(2026, 2, 2, tzinfo=ingest.timezone.utc),
                           **kwargs)


def events(path):
    return json.loads(path.read_text(encoding="utf-8")).get("events", [])


with tempfile.TemporaryDirectory(prefix="synthetic-ingest-") as temp:
    ledger, state_path, state = setup(temp)
    transport = FakeIMAP()
    first = scan(transport, ledger, state_path, state)
    assert first["scanned"] == 1 and first["candidate_messages"] == 1
    assert first["transport_complete"] is True and first["coverage"]["complete"] is False
    assert len(events(ledger)) == 1 and "body" not in events(ledger)[0]
    restart_state = ingest.load_state(state_path)
    again = scan(transport, ledger, state_path, restart_state)
    assert again["scanned"] == 0 and len(events(ledger)) == 1

# A native Message-ID collapses copies in separate folders without a new ledger revision.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-duplicates-") as temp:
    ledger, state_path, state = setup(temp)
    transport = FakeIMAP({"INBOX": {1: raw_message()}, "Archive": {9: raw_message()}})
    scan(transport, ledger, state_path, state, folders=["INBOX", "Archive"])
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(doc["events"]) == 1, doc["events"]

# UIDVALIDITY reset clears only the affected cursor and visibly starts again at UID 1.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-reset-") as temp:
    ledger, state_path, state = setup(temp)
    first = FakeIMAP()
    scan(first, ledger, state_path, state)
    state = ingest.load_state(state_path)
    reset = FakeIMAP({"INBOX": {1: raw_message("<m2@example.invalid>")}}, uidvalidity="12")
    result = scan(reset, ledger, state_path, state)
    assert result["uid_resets"] == 1 and result["scanned"] == 1
    assert len(events(ledger)) == 2

# Changed backfill windows and named repair passes receive independent cursors.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-windows-") as temp:
    ledger, state_path, state = setup(temp)
    transport = FakeIMAP()
    scan(transport, ledger, state_path, state, mode="backfill", since="2026-02-01")
    state = ingest.load_state(state_path)
    scan(transport, ledger, state_path, state, mode="backfill", since="2026-02-02")
    state = ingest.load_state(state_path)
    scan(transport, ledger, state_path, state, mode="repair", repair_name="matcher-v2")
    assert len(state["scopes"]) == 3
    assert len({key for key in state["scopes"]}) == 3

# A failed body fetch does not advance through that UID; a restart can fetch it.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-body-failure-") as temp:
    ledger, state_path, state = setup(temp)
    broken = FakeIMAP(failures={("INBOX", 1)})
    result = scan(broken, ledger, state_path, state)
    assert result["interrupted"] is True and len(state["scopes"]) == 0
    recovered = scan(FakeIMAP(), ledger, state_path, state)
    assert recovered["candidate_messages"] == 1 and len(events(ledger)) == 1

# Message caps interrupt durably and resume from the next UID.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-message-cap-") as temp:
    ledger, state_path, state = setup(temp)
    transport = FakeIMAP({"INBOX": {
        1: raw_message("<cap-1@example.invalid>"),
        2: raw_message("<cap-2@example.invalid>"),
        3: raw_message("<cap-3@example.invalid>"),
    }})
    partial = scan(transport, ledger, state_path, state, max_messages=1)
    assert partial["interrupted"] and len(events(ledger)) == 1
    state = ingest.load_state(state_path)
    resumed = scan(transport, ledger, state_path, state, max_messages=2)
    assert resumed["transport_complete"] and len(events(ledger)) == 3

# Failed ledger persistence must leave the cursor unchanged; successful retry is durable.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-ledger-failure-") as temp:
    ledger, state_path, state = setup(temp)
    def fail_persist(_path, _incoming):
        raise OSError("synthetic ledger failure")
    try:
        scan(FakeIMAP(), ledger, state_path, state, persist=fail_persist)
        raise AssertionError("ledger failure was not surfaced")
    except OSError:
        pass
    assert not state_path.exists() and len(state["scopes"]) == 0
    scan(FakeIMAP(), ledger, state_path, state)
    assert len(events(ledger)) == 1 and len(state["scopes"]) == 1

# Oversized content remains reviewable; outbound detection is exact and origin stays unknown.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-limits-") as temp:
    ledger, state_path, state = setup(temp)
    big = raw_message(body="x" * (ingest.MAX_BODY_BYTES + 100))
    result = scan(FakeIMAP({"INBOX": {1: big}}), ledger, state_path, state)
    assert result["oversized_bodies"] == 1
    item = events(ledger)[0]
    assert item["verification"] == "review_required" and "body" not in item
    assert item["origin"] == "unknown"

with tempfile.TemporaryDirectory(prefix="synthetic-ingest-outbound-") as temp:
    ledger, state_path, state = setup(temp)
    sent = raw_message("<sent@example.invalid>", sender=ADDRESS)
    scan(FakeIMAP({"INBOX": {1: sent}}), ledger, state_path, state)
    item = events(ledger)[0]
    assert item["event_type"] == "unrelated" and item["direction"] == "outbound"
    assert item["origin"] == "unknown"

# Folder limits are reported. Oversized headers retain bounded review evidence.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-folder-limit-") as temp:
    ledger, state_path, state = setup(temp)
    folders = ["F%d" % index for index in range(21)]
    transport = FakeIMAP({folder: {} for folder in folders})
    result = scan(transport, ledger, state_path, state, folders=folders)
    assert result["interrupted"] and result["excluded_folders"] == 1
    assert result["folder_limit_reached"] and result["candidate_exclusions"] == 0
    assert len(result["folder_results"]) == ingest.MAX_FOLDERS + 1
    assert result["folder_results"][-1]["reason"] == "folder_limit"

with tempfile.TemporaryDirectory(prefix="synthetic-ingest-header-limit-") as temp:
    ledger, state_path, state = setup(temp)
    transport = FakeIMAP()
    transport.headers = lambda _uid: (b"Subject: " + b"x" * 1500 + b"\r\n\r\n", 999)
    result = scan(transport, ledger, state_path, state, max_header_bytes=1024)
    assert result["candidate_exclusions"] == 0
    assert result["candidate_messages"] == 1
    assert result["folder_results"][0]["header_limit_exclusions"] == 1
    item = events(ledger)[0]
    assert item["verification"] == "review_required"
    assert item["fetch_limitation"] == "header_size_limit"
    assert item["provider_identity"] == {"account": "synthetic", "folder": "INBOX",
                                         "uidvalidity": "11", "uid": 1}
    assert "x" * 1500 not in ledger.read_text(encoding="utf-8")
    assert next(iter(ingest.load_state(state_path)["scopes"].values()))["uid"] == 1
    restarted = scan(transport, ledger, state_path, ingest.load_state(state_path),
                     max_header_bytes=1024)
    assert restarted["scanned"] == 0 and len(events(ledger)) == 1


# A deadline reached during any transport operation stops further mailbox I/O,
# including empty folders. Failed/unfinished UIDs remain replayable.
for slow_operation in ("select", "search", "headers", "body"):
    with tempfile.TemporaryDirectory(prefix="synthetic-ingest-deadline-") as temp:
        ledger, state_path, state = setup(temp)
        virtual_time = [0.0]
        calls, budgets = [], []
        transport = FakeIMAP({"INBOX": {1: raw_message()}, "Archive": {}})
        transport.set_timeout = lambda remaining: budgets.append(remaining)
        for operation in ("select", "search", "headers", "body"):
            original = getattr(transport, operation)
            def timed(*args, operation=operation, original=original):
                calls.append(operation)
                result = original(*args)
                virtual_time[0] += 2 if operation == slow_operation else 0.1
                return result
            setattr(transport, operation, timed)
        result = scan(transport, ledger, state_path, state,
                      folders=["INBOX", "Archive"], max_seconds=1,
                      monotonic=lambda: virtual_time[0])
        assert result["interrupted"] and not result["transport_complete"]
        assert calls[-1] == slow_operation and calls.count("select") == 1, calls
        assert all(0 < budget <= 1 for budget in budgets)
        assert all(row.get("reason") == "time_limit" for row in result["folder_results"])
        assert not state["scopes"] and events(ledger) == []

with tempfile.TemporaryDirectory(prefix="synthetic-ingest-empty-deadline-") as temp:
    ledger, state_path, state = setup(temp)
    virtual_time = [0.0]
    transport = FakeIMAP({"A": {}, "B": {}})
    selected = []
    def slow_select(folder):
        selected.append(folder)
        virtual_time[0] += 2
        return {"ok": True, "uidvalidity": "11"}
    transport.select = slow_select
    result = scan(transport, ledger, state_path, state, folders=["A", "B"],
                  max_seconds=1, monotonic=lambda: virtual_time[0])
    assert selected == ["A"] and result["interrupted"]
    assert not result["transport_complete"]

# The production wrapper asks for read-only SELECT and never fetches a non-PEEK body.
class FakeClient:
    def __init__(self):
        self.commands = []
    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"0"]
    def response(self, name):
        return name, [b"123"]
    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == "SEARCH":
            return "OK", [b"1"]
        if "HEADER.FIELDS" in args[-1]:
            return "OK", [(b"1 (RFC822.SIZE 4)", b"Date: x\r\n\r\n")]
        return "OK", [(b"1", b"data")]

client = FakeClient()
transport = ingest.IMAPTransport(client)
class FakeSocket:
    def settimeout(self, value):
        self.timeout = value
client.sock = FakeSocket()
transport.set_timeout(0.25)
assert client.sock.timeout == 0.25
transport.set_timeout(30)
assert client.sock.timeout == 15
assert transport.select("INBOX")["ok"]
transport.search("INBOX")
transport.headers(1)
transport.body(1, 4)
assert client.commands[0] == ("select", "INBOX", True)
assert all("BODY.PEEK" in str(command) for command in client.commands if command[0] == "FETCH")
assert any("<0.%d>" % (ingest.MAX_HEADER_BYTES + 1) in str(command)
           for command in client.commands if command[0] == "FETCH")

for invalid in ("mail.example\r\nLOGIN x", "https://example.invalid", "bad account"):
    try:
        ingest.validate_host(invalid) if "mail" in invalid or "://" in invalid else ingest.validate_account(invalid)
        raise AssertionError("unsafe configuration accepted")
    except ValueError:
        pass

# Offline normalized imports require no mailbox and never persist message bodies.
with tempfile.TemporaryDirectory(prefix="synthetic-ingest-offline-") as temp:
    root = Path(temp)
    tracker = root / "applications.yaml"
    messages = root / "messages.json"
    ledger = root / "runtime" / "events.json"
    tracker.write_text("""- company: Atlas
  role: Data Intern
  link: https://jobs.example/role?job_id=42
  applied_date: 2026-01-10
  pr: 7
""", encoding="utf-8")
    body = "Neutral synthetic note " + "private-body-canary"
    messages.write_text(json.dumps({"messages": [{
        "account": "synthetic", "message_id": "<offline@example.invalid>",
        "from": "recruiter@atlas.example", "subject": "Atlas Data Intern",
        "occurred_at": "2026-02-01T10:00:00Z", "body": body,
        "body_complete": True, "posting_url": "https://jobs.example/role?job_id=42",
    }]}), encoding="utf-8")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert ingest.main(["import", "--messages", str(messages),
                            "--tracker", str(tracker), "--ledger", str(ledger)]) == 0
    assert body not in ledger.read_text(encoding="utf-8")
    assert body not in output.getvalue()
    assert len(events(ledger)) == 1

with tempfile.TemporaryDirectory(prefix="synthetic-ingest-invalid-state-") as temp:
    bad = Path(temp) / "cursor.json"
    bad.write_text('{"schema":99,"scopes":{}}', encoding="utf-8")
    try:
        ingest.load_state(bad)
        raise AssertionError("malformed state was accepted")
    except ValueError:
        pass

print("test_email_ingest: all assertions passed")
