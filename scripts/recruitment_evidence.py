#!/usr/bin/env python3
"""Conservative recruitment evidence ledger; never sends mail or edits a tracker.

Input messages must retain exact provider identities/dates. Search hits are
discovery evidence only. A complete body from a native fetch can establish an
event, while human authorship requires separately verified evidence.
"""
import argparse
import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


def current_text(value):
    text = str(value or "")
    text = re.split(r"(?is)<blockquote\b|<div[^>]+(?:gmail_quote|yahoo_quoted)", text)[0]
    text = re.sub(r"(?is)<(?:script|style)\b.*?</(?:script|style)>", "", text)
    text = re.sub(r"(?i)<(?:br\s*/?|/p|/div)>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    kept = []
    for line in text.splitlines():
        if re.match(r"(?i)^\s*(?:>|on .+wrote:|[-_]{2,}\s*(?:original|forwarded) message|from:|--\s*$)", line):
            break
        kept.append(line)
    return re.sub(r"[ \t]+", " ", "\n".join(kept)).strip()


def safe_excerpt(value):
    text = re.sub(r"https?://\S+", "[link removed]", str(value or ""))
    text = re.sub(r"(?i)\b(?:token|password|otp|verification code|security code)\s*[:=]\s*\S+", "[secret removed]", text)
    return re.sub(r"\s+", " ", text).strip()[:420]


def exact_date(value):
    value = str(value or "").strip()
    if not re.search(r"\b20\d{2}\b", value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return dt.isoformat() if dt else None


# Outcomes require a substantive action, not a mention in a footer or an
# acknowledgement describing what might happen later.
RULES = [
    ("rejection", r"\b(?:we (?:will not be (?:moving|proceeding)|have decided not to (?:proceed|move))|your application (?:was|has been) unsuccessful|you (?:have not been|were not) (?:selected|shortlisted)|(?:position|role) has been filled|we (?:are|have) (?:decided to )?(?:moving|moved) (?:forward )?with (?:other|another) candidates?)\b"),
    ("oa_failed", r"\b(?:(?:did not|didn't|have not|haven't) pass|failed)\b.{0,80}\b(?:assessment|coding test|online test)\b|\b(?:assessment|coding test|online test)\b.{0,50}\b(?:failed|not passed|unsuccessful)\b"),
    ("oa_passed", r"\b(?:passed|successfully completed)\b.{0,80}\b(?:assessment|coding test|online test)\b|\b(?:assessment|coding test|online test)\b.{0,50}\b(?:passed|successful)\b"),
    ("oa_completed", r"\b(?:(?:completed|finished|submitted)\b.{0,80}\b(?:assessment|coding test|online test)|\b(?:assessment|coding test|online test)\b.{0,50}\b(?:completed|submitted)\b)"),
    ("oa_invitation", r"\b(?:(?:please|kindly|invite you to|invited to|invitation to).{0,100}(?:complete|take|attempt|participate in|start).{0,60}(?:assessment|coding test|online test)|(?:assessment|coding test|online test).{0,60}(?:invitation|invited|due by|deadline|must be completed))\b"),
    ("interview_completed", r"\b(?:(?:completed|finished|held|conducted)\b.{0,60}\b(?:interview|phone screen|technical screen)|\b(?:interview|phone screen|technical screen)\b.{0,50}\b(?:completed|finished|has concluded)\b)"),
    ("interview_invitation", r"\b(?:(?:invite|inviting|invited).{0,60}(?:interview|phone screen|technical screen)|(?:schedule|arrange|confirm).{0,40}(?:your|an|the) (?:interview|phone screen|technical screen)|(?:your|this|the) (?:first |second |technical )?(?:round )?interview will be (?:with|on|held|conducted)|your interview (?:is|has been) (?:scheduled|confirmed))\b"),
    ("availability_request", r"\b(?:(?:please|kindly|could you|can you|would you).{0,90}(?:availability|available (?:dates|times)|internship (?:period|dates)|graduation date|expected graduation)|(?:what|when) (?:is|are|would be) your (?:availability|earliest start|graduation))\b"),
    ("recruiter_reply", r"\b(?:please|kindly|could you|can you)\b.{0,70}\b(?:provide|send|share|confirm|clarify)\b.{0,70}(?:transcript|resume|cv|work (?:permit|authori[sz]ation)|student pass|internship|graduation|credit.bearing)\b"),
    ("offer", r"\b(?:we are pleased to offer you|we would like to offer you|we are offering you)\b.{0,100}\b(?:position|role|internship|job)\b"),
    ("acknowledgement", r"\b(?:thank (?:you|you for) (?:for )?(?:applying|your application|your interest)|we (?:have )?received your application|application (?:has been |was )?(?:received|submitted)|we.ve got your application|video interview received)\b"),
]


def classify_message(message):
    body = re.sub(r"\s+", " ", current_text(message.get("body") or message.get("text") or ""))
    subject = str(message.get("subject") or "")
    sender = str(message.get("from") or message.get("sender") or "")
    blob = subject + "\n" + body
    kind, evidence = "unclassified", "No substantive outcome established"
    if message.get('direction')=='outbound':
        kind,evidence='unrelated','Candidate-authored outgoing message; not a recruiter outcome'
    elif re.search(r"(?i)reset.{0,35}password|password reset|(?:login|sign.in|verification|security) code|verify your (?:candidate )?account|welcome for creating an account", subject):
        kind, evidence = "account_admin", "Account administration subject"
    elif re.search(r"(?i)job(?:s|alerts?)[-_.]?(?:noreply|notification)|@(?:match\.)?indeed\.com", sender) or re.search(r"(?i)new jobs (?:posted|similar)|job alert|this job is a match|is hiring[: ]", subject):
        kind, evidence = "job_alert", "Job alert sender/subject"
    else:
        for candidate, pattern in RULES:
            # Actionable outcomes need current body; acknowledgements can be
            # recognized from the subject but remain unverified without a body.
            source = blob if candidate == "acknowledgement" else body
            hit = re.search(pattern, source, re.I | re.S)
            if hit and candidate not in ("acknowledgement", "rejection"):
                # Conditional recruitment-process descriptions are common in
                # acknowledgements. They do not establish a current invite.
                context = re.split(r"[.!?\n]", source[:hit.start()])[-1] + hit.group()
                if re.search(r"\b(?:if|may|might|shortlisted candidates|successful candidates|selected candidates)\b", context, re.I):
                    continue
            if hit:
                kind, evidence = candidate, safe_excerpt(hit.group())
                break
    occurred = exact_date(message.get("occurred_at") or message.get("date"))
    mid = str(message.get("message_id") or "").strip()
    stable = bool(mid and not mid.startswith(("sha1:", "weak:")) and not mid.isdigit())
    verified = (bool(body) and message.get("body_complete") is True and
                stable and occurred is not None and kind != "unclassified")
    reasons = []
    if not body or message.get("body_complete") is not True:
        reasons.append("complete current message body unavailable")
    if not stable:
        reasons.append("stable message identity unavailable")
    if occurred is None:
        reasons.append("exact occurrence date unavailable")
    if kind == "unclassified":
        reasons.append("no explicit current-message recruitment event")
    origin = "unknown"
    if message.get("origin_verification") == "verified" and message.get("origin") in ("human", "automated"):
        origin = message["origin"]
    return {"event_type": kind, "verification": "verified" if verified else "review_required",
            "origin": origin, "evidence": evidence, "occurred_at": occurred,
            "verification_reasons": reasons,
            "body_sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()}


WATCHER_ENVELOPE_SCHEMA = 1
EVENT_TYPES = {"rejection", "oa_invitation", "interview_invitation", "unrelated",
               "availability_request", "recruiter_reply", "offer",
               "oa_completed", "oa_passed", "oa_failed", "interview_completed",
               "acknowledgement", "account_admin", "job_alert", "unclassified"}


def _event_id(account, message_id):
    return "mail:" + hashlib.sha256((str(account or "default") + "|" + str(message_id)).encode()).hexdigest()


def _envelope_digest(envelope):
    payload = {k: envelope[k] for k in (
        "schema", "source", "account", "message_id", "event_id", "body_sha256",
        "event_type", "verification", "origin", "origin_verification",
        "evidence", "occurred_at", "verification_reasons")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_watcher_envelope(message):
    """Build a body-private classified event envelope for gmail_watch JSON."""
    body = message.get("body") or message.get("text") or ""
    mid = str(message.get("message_id") or "").strip()
    account = str(message.get("account") or "default")
    classified = classify_message(message)
    envelope = {
        "schema": WATCHER_ENVELOPE_SCHEMA, "source": "gmail_watch",
        "account": account, "message_id": mid,
        "event_id": _event_id(account, mid) if mid else "",
        "body_sha256": hashlib.sha256(str(body).encode("utf-8", "replace")).hexdigest(),
        "event_type": classified["event_type"], "verification": classified["verification"],
        "origin": classified["origin"], "origin_verification": message.get("origin_verification", ""),
        "evidence": safe_excerpt(classified["evidence"]),
        "occurred_at": classified["occurred_at"],
        "verification_reasons": classified.get("verification_reasons", []),
    }
    envelope["digest"] = _envelope_digest(envelope)
    return envelope


def trusted_watcher_envelope(message):
    """Validate the explicit local watcher format; never trust top-level flags alone.

    The digest detects accidental mutation, not authenticity. External JSON
    still requires a trusted ingestion path.
    """
    envelope = message.get("producer_envelope")
    if not isinstance(envelope, dict):
        return None
    if envelope.get("schema") != WATCHER_ENVELOPE_SCHEMA or envelope.get("source") != "gmail_watch":
        return None
    mid = str(message.get("message_id") or "").strip()
    if (not mid or mid.startswith(("sha1:", "weak:")) or mid.isdigit()
            or envelope.get("message_id") != mid):
        return None
    if envelope.get("event_id") != _event_id(envelope.get("account"), mid):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", str(envelope.get("body_sha256") or "")):
        return None
    body = message.get("body") or message.get("text") or ""
    if hashlib.sha256(str(body).encode("utf-8", "replace")).hexdigest() != envelope["body_sha256"]:
        return None
    if envelope.get("event_type") not in EVENT_TYPES:
        return None
    if envelope.get("verification") not in {"verified", "review_required"}:
        return None
    if envelope.get("origin") not in {"human", "automated", "unknown"}:
        return None
    if envelope.get("origin") != "unknown" and envelope.get("origin_verification") != "verified":
        return None
    if envelope.get("verification") == "verified" and not envelope.get("occurred_at"):
        return None
    if envelope.get("evidence") != safe_excerpt(envelope.get("evidence")):
        return None
    if envelope.get("digest") != _envelope_digest(envelope):
        return None
    # Duplicated top-level fields are convenience only and must agree with the
    # private envelope wherever present.
    for key in ("event_type", "verification", "origin", "occurred_at", "evidence"):
        if key in message and message[key] != envelope[key]:
            return None
    if "classification_verified" in message and message["classification_verified"] != (envelope["verification"] == "verified"):
        return None
    return {key: envelope[key] for key in ("event_id", "message_id", "event_type",
            "verification", "origin", "evidence", "occurred_at", "body_sha256",
            "verification_reasons")}


VERSION_FIELDS = {"revision", "revision_id", "supersedes", "recorded_at", "adjudicated_at"}


def _revision_id(event):
    payload = {k: v for k, v in event.items() if k not in VERSION_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _semantic(event):
    return tuple(event.get(k) for k in ("event_type", "application_id", "occurred_at", "origin"))


def _version(event, revision=1, supersedes=None):
    out = dict(event)
    out["revision"] = int(out.get("revision") or revision)
    computed_id = _revision_id(out)
    if out.get("revision_id") and out["revision_id"] != computed_id:
        raise ValueError("event revision_id does not match event payload")
    out["revision_id"] = computed_id
    out["supersedes"] = list(out.get("supersedes") or supersedes or [])
    return out


def effective_events(doc_or_events):
    """Return ``(effective_events, quarantined_ids, diagnostics)``.

    A review-required revision can be superseded by a verified revision when
    any known event/application/date fields agree. Multiple contradictory
    verified leaves remain visible and their event ID is quarantined.
    """
    events = doc_or_events.get("events", []) if isinstance(doc_or_events, dict) else doc_or_events
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    grouped = {}
    diagnostics = {"duplicate_event_revisions": 0, "superseded_revisions": 0,
                   "quarantined_event_ids": 0, "events_missing_event_id": 0}
    for raw in events:
        if not isinstance(raw, dict) or not str(raw.get("event_id") or "").strip():
            diagnostics["events_missing_event_id"] += 1
            continue
        ev = _version(raw)
        group = grouped.setdefault(str(ev["event_id"]), {})
        prior = group.get(ev["revision_id"])
        if prior is not None:
            diagnostics["duplicate_event_revisions"] += 1
            continue
        group[ev["revision_id"]] = ev
    selected, quarantined = [], set()
    for event_id, revisions in grouped.items():
        valid_ids = set(revisions)
        superseded = {rid for event in revisions.values() for rid in event["supersedes"]
                      if rid in valid_ids and rid != event["revision_id"]}
        leaves = [event for rid, event in revisions.items() if rid not in superseded]
        if not leaves:
            leaves = list(revisions.values())
        diagnostics["superseded_revisions"] += max(0, len(revisions) - len(leaves))
        verified = [event for event in leaves if event.get("verification") == "verified"]
        if len(verified) > 1:
            semantics = {_semantic(event) for event in verified}
            if len(semantics) > 1:
                quarantined.add(event_id)
                diagnostics["quarantined_event_ids"] += 1
                selected.extend(sorted(revisions.values(), key=lambda e: e["revision"]))
                continue
            # Identical substantive events may differ only by an approved
            # origin adjudication; prefer more informative origin deterministically.
            origin_rank = {"unknown": 0, "automated": 1, "human": 1}
            chosen = max(verified, key=lambda e: (origin_rank.get(e.get("origin"), 0), e["revision"]))
            selected.append(chosen)
            continue
        if verified:
            selected.append(verified[0])
            continue
        selected.append(max(leaves, key=lambda e: e["revision"]))
    return selected, quarantined, diagnostics


def match_application(message, tracker_rows):
    """Use explicit current-body role text to disambiguate an existing match.

    The existing sender/company rules still apply. Body-only fallback requires
    an exact role phrase; quoted text and company mentions alone cannot create
    a new application match.
    """
    import application_identity as identity
    import stages
    apps=[]
    for row in tracker_rows:
        role=str(row.get('role') or '')
        role=re.sub(r'(?i)^\[(?:20\d{2}\s+H[12]\s+)?(?:term[- ]time\s+)?internship\]\s*','',role)
        role=re.sub(r'(?i)\s+\(20\d{2}\s+H[12]\s+term[- ]time\)$','',role)
        apps.append({'row':dict(row,role=role),'identity_row':row})
    sender = message.get("from") or message.get("sender") or ""
    subject = str(message.get("subject") or "")
    url = message.get("posting_url") or message.get("link")
    if url:
        normalized = identity.normalize_posting_url(url)
        if not normalized:
            return None, "posting URL is invalid"
        hits = [entry for entry in apps
                if identity.normalize_posting_url(entry["identity_row"].get("link")) == normalized]
        if len(hits) == 1:
            return hits[0], "exact posting URL"
        if len(hits) > 1:
            return None, "posting URL matches multiple applications"
    if message.get("body_complete") is not True:
        return None, "complete current message body unavailable for exact role match"
    body = current_text(message.get("body") or message.get("text") or "")
    if not body:
        return None, "complete current message body is empty"
    # A sender domain or employer name alone is not an application identity.
    # Require one exact employer/role pair in the subject plus current body.
    matched, reason = stages.match_row(apps, sender, subject + "\n" + body)
    if matched is None:
        return None, reason
    return matched, reason + "; exact role in current message"


def reconcile(messages, tracker_rows, coverage=None):
    import application_identity as identity
    import stages
    events, review = [], []
    for message in messages:
        trusted = trusted_watcher_envelope(message)
        event = trusted or classify_message(message)
        mid = str(message.get("message_id") or "")
        scope = str(message.get("account") or "default")
        identity_key = mid or json.dumps([message.get(k) for k in ("from", "subject", "date")])
        event_id = event.get("event_id") or _event_id(scope, identity_key)
        event.update(event_id=event_id, message_id=mid, application_id="",
                     subject=("[account administration message]" if event["event_type"] == "account_admin"
                              else safe_excerpt(message.get("subject"))), sender=message.get("from", ""),
                     source=message.get("source") or ("gmail_watch" if trusted else "unknown"))
        if message.get('direction') in ('inbound','outbound'):event['direction']=message['direction']
        provider_identity = message.get("provider_identity")
        if isinstance(provider_identity, dict):
            event["provider_identity"] = {key: provider_identity.get(key) for key in
                ("account", "folder", "uidvalidity", "uid", "gmail_message_id",
                 "rfc822_message_id", "provider_message_id") if provider_identity.get(key) is not None}
        limitation = message.get("fetch_limitation") or message.get("fetch_limitations")
        if limitation:
            event["fetch_limitation"] = safe_excerpt(
                "; ".join(str(x) for x in limitation) if isinstance(limitation, list) else limitation)
        # The matcher abstains for ambiguous company/role matches. Caller
        # application_id claims are deliberately not trusted as a join.
        matched, reason = match_application(message,tracker_rows)
        if matched is not None:
            event["application_id"] = identity.application_id(
                matched.get("identity_row", matched["row"]))
            event['application_match_basis']=reason
        else:
            event["verification"] = "review_required"
            event["match_reason"] = reason
        if not event["application_id"] or event["verification"] != "verified":
            review.append(event_id)
        events.append(_version(event))
    # Keep conflicting duplicates for the report's explicit collision check.
    unique = {}
    for event in events:
        unique[json.dumps(event, sort_keys=True)] = event
    effective, quarantined, _ = effective_events(list(unique.values()))
    review = {str(event.get("event_id")) for event in effective
              if event.get("verification") != "verified" or not event.get("application_id")}
    review.update(quarantined)
    return {"schema": 1, "coverage": coverage or {"complete": False},
            "events": list(unique.values()), "review_required": sorted(review)}


def merge_ledgers(previous, incoming):
    if previous.get("schema") != 1 or not isinstance(previous.get("events"), list):
        raise ValueError("Refusing to overwrite an invalid ledger")
    if incoming.get("schema") != 1 or not isinstance(incoming.get("events"), list):
        raise ValueError("Refusing to merge an invalid incoming ledger")
    events = [_version(event) for event in previous["events"]]
    for raw in incoming["events"]:
        if not isinstance(raw, dict) or not str(raw.get("event_id") or "").strip():
            continue
        candidate = _version(raw)
        same_id = [event for event in events if event.get("event_id") == candidate["event_id"]]
        if any(event["revision_id"] == candidate["revision_id"] for event in same_id):
            continue
        current_events, quarantined, _ = effective_events(same_id)
        current = current_events[0] if len(current_events) == 1 and candidate["event_id"] not in quarantined else None
        supersedes = []
        if current is not None:
            old_verified = current.get("verification") == "verified"
            new_verified = candidate.get("verification") == "verified"
            old_semantic = _semantic(current)
            new_semantic = _semantic(candidate)
            known_conflict = any(a not in (None, "", "unclassified", "unknown")
                                 and b not in (None, "", "unclassified", "unknown")
                                 and a != b for a, b in zip(old_semantic, new_semantic))
            if old_verified and not new_verified:
                # A thinner reimport cannot replace already verified content.
                continue
            if known_conflict:
                pass  # preserve both as sibling revisions; reporter quarantines
            elif (not old_verified and new_verified):
                supersedes = [current["revision_id"]]
            elif old_verified and new_verified and old_semantic == new_semantic:
                if candidate.get("evidence") == current.get("evidence"):
                    # Same event and evidence with only nonsemantic metadata changes.
                    supersedes = [current["revision_id"]]
        # Explicit manual adjudication may resolve a previously quarantined
        # event only when it names every live conflicting revision and states
        # why. This is never inferred from an origin label alone.
        if candidate.get("source") == "manual_adjudication":
            all_ids = {event["revision_id"] for event in same_id}
            live_ids = {rid for rid in all_ids
                        if not any(rid in other.get("supersedes", []) for other in same_id)}
            requested = set(candidate.get("supersedes", []))
            same_event_origin_only = (current is not None
                and old_semantic[:3] == new_semantic[:3]
                and current.get("origin") == "unknown"
                and candidate.get("origin") in {"human", "automated"}
                and bool(str(candidate.get("evidence") or "").strip()))
            explicit_correction = (candidate.get("resolution") == "explicit"
                                   and bool(str(candidate.get("evidence") or "").strip()))
            if live_ids and requested == live_ids and (same_event_origin_only or explicit_correction):
                supersedes = sorted(live_ids)
        candidate["revision"] = max((e.get("revision", 0) for e in same_id), default=0) + 1
        candidate["revision_id"] = _revision_id(candidate)
        candidate["supersedes"] = supersedes
        events.append(candidate)
    effective, quarantined, _ = effective_events(events)
    review_required = {str(event.get("event_id")) for event in effective
                       if event.get("verification") != "verified" or not event.get("application_id")}
    review_required.update(quarantined)
    out = {"schema": 1, "events": events,
            "coverage": merge_coverage(previous.get("coverage", {}), incoming.get("coverage", {})),
            "review_required": sorted(review_required)}
    return out


def _flatten_coverage(value):
    records = []
    limitations = []
    if not isinstance(value, dict):
        return records, limitations
    has_sources = isinstance(value.get("sources"), list)
    if has_sources:
        for child in value["sources"]:
            child_records, child_limits = _flatten_coverage(child)
            records.extend(child_records)
            limitations.extend(child_limits)
    record = {k: v for k, v in value.items() if k not in {"sources", "limitations", "history"}}
    if ((not has_sources or record.get("folder"))
            and any(k in record for k in ("source", "account", "folder", "transport_complete", "scanned_folders"))):
        records.append(record)
    for item in value.get("limitations", []) if isinstance(value.get("limitations"), list) else []:
        if isinstance(item, str) and item.strip():
            limitations.append(item.strip())
    if isinstance(value.get("limitation"), str) and value["limitation"].strip():
        limitations.append(value["limitation"].strip())
    return records, limitations


def merge_coverage(previous, incoming):
    records, old_limits = _flatten_coverage(previous)
    new_records, new_limits = _flatten_coverage(incoming)
    by_source = {}
    for item in records + new_records:
        key = tuple(str(item.get(k) or "") for k in ("source", "account", "folder"))
        by_source[key] = item
    sources = [by_source[key] for key in sorted(by_source)][-32:]
    limitations = list(dict.fromkeys(old_limits + new_limits))[-20:]
    # Completeness remains false unless one explicit source asserts complete
    # coverage and no subsequent source has downgraded it.
    complete = bool(isinstance(incoming, dict) and incoming.get("complete") is True)
    out = {"complete": complete, "sources": sources, "limitations": limitations}
    if isinstance(incoming, dict):
        for key in ("transport_complete", "scanned_folders", "account", "source",
                    "last_scan_at", "message_count", "uidvalidity", "uid_next"):
            if key in incoming:
                out[key] = incoming[key]
    return out


def save_ledger(output, result):
    # A separate persistent lock protects concurrent importers. The production
    # target is Linux; the Windows branch supports offline use as well.
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(str(output) + ".lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0"); lock.flush()
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if output.exists():
                result = merge_ledgers(json.loads(output.read_text(encoding="utf8")), result)
            else:
                result = merge_ledgers({"schema":1,"events":[],"coverage":{}}, result)
            tmp = output.with_suffix(output.suffix + ".tmp")
            with open(tmp, "w", encoding="utf8") as stream:
                os.chmod(tmp, 0o600)
                stream.write(json.dumps(result, indent=2) + "\n")
                stream.flush(); os.fsync(stream.fileno())
            tmp.replace(output)
        finally:
            if os.name == "nt":
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return result


def main(argv=None):
    from config import default_repo_dir, default_runtime_path, path_setting
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", required=True,
                        help="Normalized message JSON; no mailbox or notification state is accessed")
    parser.add_argument("--tracker", default=path_setting(
        "JOBSEARCH_TRACKER", Path(default_repo_dir()) / "applications.yaml"))
    parser.add_argument("--output", default=path_setting(
        "JOBSEARCH_RECRUITMENT_LEDGER", default_runtime_path("recruitment_events.json")),
        help="Private evidence ledger outside the checkout")
    args = parser.parse_args(argv)
    import summary
    doc = json.loads(Path(args.messages).read_text(encoding="utf8"))
    messages = doc.get("messages", []) if isinstance(doc, dict) else doc
    rows = summary.parse_yaml_list(Path(args.tracker).read_text(encoding="utf8"))
    result = reconcile(messages, rows, doc.get("coverage") if isinstance(doc, dict) else None)
    output = Path(args.output)
    result = save_ledger(output, result)
    print(json.dumps({"events": len(result["events"]), "review_required": len(result["review_required"])}))
    return 0


if __name__ == "__main__":
    main()
