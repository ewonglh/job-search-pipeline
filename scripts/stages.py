#!/usr/bin/env python3
"""Email-driven stage machine for the application tracker (stdlib only).

The tracker (``repo/applications.yaml``) is a hand-maintained top-level YAML
list: 90 flat-keyed mappings, comment lines, and folded multi-line values
(``  notes: text`` followed by 4-space indented continuation lines). This
script therefore never round-trips the file through a parser: it edits it
**line by line**, so comments, key order, folded values and unrelated keys all
survive untouched. Writes are atomic (temp file + ``os.replace``).

Stages come from ``summary.py`` (the canonical funnel and the alias table live
there; this file imports them so the two cannot drift apart):

  ``Found`` -> ``Tailored`` -> ``Applied`` -> ``Callback`` -> ``OA`` ->
  ``Interview`` -> ``Accepted`` | ``Rejected``

``Callback`` and ``OA`` are optional, so a jump from ``Applied`` straight to
``Interview`` is legal and nothing here treats the skipped stages as missing.

Row provenance written by ``set`` / ``apply-email`` (all optional keys):

  ``stage``           canonical stage; authoritative when present
  ``stage_since``     ISO date the current stage started
  ``stage_source``    ``email`` | ``queue`` | ``manual``
  ``stage_evidence``  one-line quote or subject reference for the transition

Every transition appends one ``date source: evidence`` segment to
``stage_evidence`` (joined with `` | ``), older segments are dropped when the
value grows past ``EVIDENCE_LIMIT``. The key is a single line by design: the
tracker's folded ``notes`` blocks are never disturbed.

Guards: a backwards transition is refused without ``--force``, and a row
already ``Rejected`` is not touched without ``--force``.

Usage:
  stages.py list [--json] [--tracker FILE] [--today YYYY-MM-DD]
  stages.py set --company X [--role Y] --stage S [--date YYYY-MM-DD]
                [--source {email,queue,manual}] [--evidence TEXT]
                [--tracker FILE] [--dry-run] [--force] [--json]
  stages.py apply-email --json FILE [--commit] [--tracker FILE]
                        [--today YYYY-MM-DD] [--json-out]

``apply-email`` reads the ``--json`` output of the browser mail lane
(``gmail_browser.py``): ``{"new": [...], "unmatched": [...]}``, or a bare list
of the same message dicts. It maps a category to a stage:

  ``rejection``    -> ``Rejected``
  ``oa``           -> ``OA``
  ``interview``    -> ``Interview``
  ``follow_up``    -> ``Callback`` only when the mail is a recruiter asking for
                      information; a generic "any update" follow-up is listed
                      as ambiguous instead of guessed

Matching is by sender domain and company name, in that order. Mail that
matches no application, or matches by a generic provider domain only, is
listed and never guessed. Dry run is the default; ``--commit`` writes.
"""
import argparse
import json
import os
import re
import sys
from datetime import date

import summary as s

HERE = os.path.dirname(os.path.abspath(__file__))
TRACKER = os.environ.get("JOBSEARCH_TRACKER",
                         os.path.join(HERE, "repo", "applications.yaml"))

STAGE_KEYS = ("stage", "stage_since", "stage_source", "stage_evidence")
SOURCES = ("email", "queue", "manual")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A key line: optional "- " list marker, a bare key, then ":" and the value.
KEY_RE = re.compile(r"^(\s*)(?:- )?([A-Za-z0-9_.\-]+):(\s?.*)$")
MAX_CONT_KEY_INDENT = 2
EVIDENCE_LIMIT = 160

# Category -> stage for the mail lane.
CATEGORY_STAGE = {
    "rejection": "Rejected",
    "oa": "OA",
    "interview": "Interview",
}

# Words that make a ``follow_up`` mail a recruiter asking for information
# rather than the applicant (or an auto-responder) chasing an update.
INFO_REQUEST_TERMS = (
    "information", "document", "documents", "transcript", "clarify",
    "clarification", "provide", "provide us", "complete", "kindly", "please",
    "availability", "available", "expected salary", "resume", "cv",
    "identification", "pass", "details", "questionnaire", "form",
)

# Free-mail / ATS provider domains: a match here is not a company identity.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "icloud.com", "protonmail.com", "proton.me",
    "greenhouse.io", "lever.co", "workday.com", "myworkday.com",
    "smartrecruiters.com", "successfactors.com", "sapsf.com",
    "linkedin.com", "indeed.com", "internsg.com", "glassdoor.com",
    "workable.com", "bamboohr.com", "recruitee.com", "jobvite.com",
    "icims.com", "taleo.net", "ashbyhq.com", "rippling.com",
    "mail.gmail.com", "notifications", "noreply", "no-reply",
}

# Legal suffixes dropped before comparing a company name to a mail sender.
COMPANY_SUFFIXES = {
    "pte", "ltd", "limited", "inc", "corp", "corporation", "co", "llc",
    "gmbh", "plc", "holdings", "holding", "group", "pte.", "the",
}


# ---------------------------------------------------------------- text utils
def norm(value):
    """Collapse whitespace, keeps the value a single line."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slug(value):
    return re.sub(r"[^a-z0-9]+", "", norm(value).lower())


def parse_day(value):
    return s.parse_date(value)


def company_tokens(name):
    """Alphanumeric tokens of a company name, legal suffixes removed."""
    words = [w for w in re.split(r"[^a-z0-9]+", norm(name).lower()) if w]
    kept = [w for w in words if w not in COMPANY_SUFFIXES]
    return kept or words


def sender_domain(sender):
    """Domain of a ``From`` header ("Name <a@b.com>" or "a@b.com")."""
    m = re.search(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+)", str(sender or ""))
    if not m:
        return ""
    return m.group(1).lower().strip(".")


# ---------------------------------------------------------- tracker (lines)
def split_records(lines):
    """Group tracker lines into records of ``{start, end, fields}``.

    ``start``/``end`` are line indexes (``end`` exclusive) of the whole record;
    ``fields`` maps key -> ``{"line": i, "inline": bool, "last": i}`` where
    ``last`` is the final continuation line belonging to that key. Comment
    lines and blank lines are part of the record they sit in and are never
    rewritten.
    """
    records = []
    cur = None
    for i, raw in enumerate(lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0 and raw.startswith("- "):
            cur = {"start": i, "end": i + 1, "fields": {}}
            records.append(cur)
            m = KEY_RE.match(raw)
            if m:
                cur["fields"][m.group(2)] = {
                    "line": i, "inline": bool(m.group(3).strip()),
                    "last": i}
            continue
        m = KEY_RE.match(raw)
        if cur is not None and m and indent <= MAX_CONT_KEY_INDENT:
            key = m.group(2)
            cur["fields"][key] = {
                "line": i, "inline": bool(m.group(3).strip()), "last": i}
            cur["end"] = i + 1
            continue
        if cur is not None:
            # Continuation line of the previous key (folded value, URL wrap).
            if cur["fields"]:
                last_key = max(cur["fields"], key=lambda k: cur["fields"][k]["line"])
                cur["fields"][last_key]["last"] = i
            cur["end"] = i + 1
    return records


def record_scalar(lines, rec, key):
    """Read a scalar out of a record the same way ``summary.parse_yaml_list``
    would: inline value plus folded continuation lines joined by a space."""
    field = rec["fields"].get(key)
    if not field:
        return None
    m = KEY_RE.match(lines[field["line"]])
    parts = [m.group(3).strip()] if m and m.group(3).strip() else []
    for j in range(field["line"] + 1, field["last"] + 1):
        parts.append(lines[j].strip())
    text = " ".join(p for p in parts if p)
    return s._unq(text) if text else None


def read_apps(path):
    """Parse the tracker into ``[{record, row}]`` in file order."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    out = []
    for rec in split_records(lines):
        row = {}
        for key in rec["fields"]:
            row[key] = record_scalar(lines, rec, key)
        out.append({"record": rec, "row": row})
    return lines, out


def stage_of(row):
    """Canonical stage of a row: ``stage`` wins over ``status``."""
    return s.row_stage(row)


def since_of(row):
    """Date the current stage started (``stage_since``, else ``applied_date``)."""
    since = s.parse_date(row.get("stage_since"))
    if since:
        return since
    if stage_of(row) in ("Found", "Tailored", "Applied"):
        return s.parse_date(row.get("applied_date"))
    return None


# ---------------------------------------------------------------- rendering
def scalar_out(value):
    """Render one tracker scalar, in the tracker's own hand-written style."""
    text = norm(value)
    if text == "":
        return '""'
    if re.fullmatch(r"[+-]?\d+", text):
        return text
    if ("#" in text or ": " in text or text[-1] == ":"
            or text.lower() in ("true", "false", "null", "~")
            or text[0] in "-?:,[]{}#&*!|>'\"%@`"):
        return "'" + text.replace("'", "''") + "'"
    return text


def render_field(key, value):
    return f"  {key}: {scalar_out(value)}"


# ---------------------------------------------------------------- evidence
def evidence_segment(stage, day, source, text):
    """One provenance segment: ``2026-10-31 email: subject line``."""
    text = norm(text)
    return (f"{day} {source or 'manual'}"
            + (f": {text}" if text else f": {stage}"))


def merge_evidence(prev, segment):
    """Append a segment, dropping the oldest until the value fits the limit."""
    parts = [p.strip() for p in norm(prev).split(" | ") if p.strip()]
    if segment in parts:
        return " | ".join(parts)
    parts.append(segment)
    while len(parts) > 1 and len(" | ".join(parts)) > EVIDENCE_LIMIT:
        parts.pop(0)
    joined = " | ".join(parts)
    return joined[:EVIDENCE_LIMIT].rstrip(" |") if len(joined) > EVIDENCE_LIMIT \
        else joined


# ---------------------------------------------------------------- guards
def transition_errors(current, new, force=False):
    """Reasons this transition is refused; empty list means it is allowed."""
    errs = []
    if force:
        return errs
    if current == "Rejected" and new != current:
        errs.append("row is already Rejected (pass --force to reopen or move)")
    elif current and new != current:
        if s.stage_index(new) < s.stage_index(current):
            errs.append(f"backwards transition {current} -> {new} "
                        "(pass --force to allow)")
    return errs


# ---------------------------------------------------------------- list
def list_rows(apps, today=None):
    today = today or date.today()
    out = []
    for entry in apps:
        row = entry["row"]
        stage = stage_of(row)
        since = since_of(row)
        days = (today - since).days if since else None
        policy = s.NUDGE_POLICY.get(stage)
        due_in = None if (days is None or policy is None) else policy - days
        out.append({
            "company": norm(row.get("company")),
            "role": norm(row.get("role")),
            "stage": stage,
            "status": norm(row.get("status")),
            "stage_since": since.isoformat() if since else norm(row.get("stage_since")),
            "stage_source": norm(row.get("stage_source")),
            "evidence": norm(row.get("stage_evidence")),
            "days": days,
            "due_in": due_in,
            "stale": bool(due_in is not None and due_in <= 0),
        })
    return out


def render_row(item):
    stage = item["stage"] or "?"
    days = "-" if item["days"] is None else f"{item['days']}d"
    since = item["stage_since"] or "-"
    ident = f"{item['company']} · {item['role']}"
    flag = ""
    if item["stale"]:
        overdue = abs(item["due_in"])
        flag = "  overdue" + (f" {overdue}d" if item["stage"] != "Accepted" else "")
    elif item["due_in"] is not None:
        flag = f"  due in {item['due_in']}d"
    src = item["stage_source"] or "-"
    return f"{stage:<10} {days:>5}  {since}  {src:<6} {ident}{flag}"


def cmd_list(args, log):
    lines, apps = read_apps(args.tracker)
    items = list_rows(apps, parse_day(args.today) or date.today())
    if args.stage:
        items = [i for i in items if i["stage"] == args.stage]
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    for item in items:
        print(render_row(item))
    return 0


# ---------------------------------------------------------------- set
def find_rows(apps, company, role=None):
    want = slug(company)
    hits = [e for e in apps if slug(e["row"].get("company")) == want]
    if role:
        hits = [e for e in hits if slug(e["row"].get("role")) == slug(role)]
    return hits


def plan_update(lines, rec, stage, day, source, evidence, force):
    """New line list for one record, or ``(None, errors)`` when refused."""
    row = {}
    for key in rec["fields"]:
        row[key] = record_scalar(lines, rec, key)
    current = stage_of(row)
    errs = transition_errors(current, stage, force)
    if errs:
        return None, errs

    new_fields = {
        "stage": stage,
        "stage_since": day,
        "stage_source": source,
        "stage_evidence": merge_evidence(
            row.get("stage_evidence"),
            evidence_segment(stage, day, source, evidence)),
    }

    edits = {}          # line index -> replacement text
    inserts = {}        # line index -> [new lines] appended after that line
    for key, value in new_fields.items():
        field = rec["fields"].get(key)
        if field:
            head = lines[field["line"]]
            prefix = "- " if head.startswith("- ") \
                else head[:len(head) - len(head.lstrip(" "))]
            edits[field["line"]] = f"{prefix}{key}: {scalar_out(value)}"
            for j in range(field["line"] + 1, field["last"] + 1):
                edits[j] = None   # drop folded continuations of the old value
        else:
            inserts.setdefault(rec["end"] - 1, []).append(render_field(key, value))
    return (edits, inserts), []


def apply_edits(lines, edits, inserts):
    out = []
    for i, text in enumerate(lines):
        if i in edits:
            if edits[i] is not None:
                out.append(edits[i])
        else:
            out.append(text)
        for extra in inserts.get(i, []):
            out.append(extra)
    return out


def write_lines(path, lines):
    text = "\n".join(lines) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def cmd_set(args, log):
    if not DATE_RE.match(args.date or ""):
        log("stages: --date must be YYYY-MM-DD")
        return 2
    stage = s.canonical_stage(args.stage)
    if not stage:
        log(f"stages: unknown stage {args.stage!r}; "
            f"expected one of {', '.join(s.ORDER)}")
        return 2
    lines, apps = read_apps(args.tracker)
    hits = find_rows(apps, args.company, args.role)
    if not hits:
        log(f"stages: no row for company {args.company!r}"
            + (f" role {args.role!r}" if args.role else ""))
        return 1
    if len(hits) > 1:
        names = "; ".join(norm(e["row"].get("role")) for e in hits)
        log(f"stages: {len(hits)} rows match {args.company!r} "
            f"({names}); pass --role")
        return 1

    entry = hits[0]
    plan, errs = plan_update(lines, entry["record"], stage, args.date,
                             args.source, args.evidence, args.force)
    if errs:
        for err in errs:
            log(f"stages: refused: {err}")
        return 3

    edits, inserts = plan
    out_lines = apply_edits(lines, edits, inserts)

    payload = {
        "company": norm(entry["row"].get("company")),
        "role": norm(entry["row"].get("role")),
        "from": stage_of(entry["row"]),
        "to": stage,
        "stage_since": args.date,
        "stage_source": args.source,
        "stage_evidence": merge_evidence(
            entry["row"].get("stage_evidence"),
            evidence_segment(stage, args.date, args.source, args.evidence)),
        "written": not args.dry_run,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        verb = "would set" if args.dry_run else "set"
        print(f"{verb} {payload['company']} · {payload['role']}: "
              f"{payload['from'] or '?'} -> {stage} "
              f"({args.date}, {args.source})")
        for i in sorted(edits):
            print(f"  -{lines[i]}")
            if edits[i] is not None:
                print(f"  +{edits[i]}")
        for i in sorted(inserts):
            for extra in inserts[i]:
                print(f"  +{extra}")
    if args.dry_run:
        if not args.json:
            print("dry run: nothing written (drop --dry-run to write)")
        return 0
    write_lines(args.tracker, out_lines)
    return 0


# ---------------------------------------------------------------- apply-email
def load_mail(path):
    """Message list out of a ``gmail_browser.py --json`` file."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict):
        msgs = []
        for key in ("new", "messages", "items", "unmatched"):
            block = doc.get(key)
            if isinstance(block, list):
                msgs.extend(m for m in block if isinstance(m, dict))
        return msgs
    if isinstance(doc, list):
        return [m for m in doc if isinstance(m, dict)]
    return []


def message_stage(msg):
    """Stage proposed by one mail, plus a flag when the mail is ambiguous."""
    category = norm(msg.get("category")).lower()
    subject = norm(msg.get("subject")).lower()
    if category in CATEGORY_STAGE:
        return CATEGORY_STAGE[category], None
    if category == "follow_up":
        if any(term in subject for term in INFO_REQUEST_TERMS) or "?" in subject:
            return "Callback", None
        return None, "follow_up without an information request"
    return None, f"category {category or 'none'} has no stage"


def match_row(apps, sender, subject=""):
    """Match a mail to one application: sender domain first, then company name.

    Returns ``(entry, basis)`` or ``(None, reason)``. A generic provider or ATS
    domain is never enough on its own, so those fall through to the subject /
    company-name test and, failing that, stay unmatched.
    """
    domain = sender_domain(sender)
    label = domain.split(".")[0] if domain and "." in domain else ""
    label = slug(label)
    subject_slug = slug(subject)

    if domain and domain not in GENERIC_DOMAINS:
        hits = [e for e in apps
                if label and label in company_tokens(e["row"].get("company"))]
        if len(hits) == 1:
            return hits[0], f"sender domain {domain}"
        if len(hits) > 1:
            return None, f"sender domain {domain} matches {len(hits)} rows"

    by_name = [e for e in apps if slug(e["row"].get("company"))
               and slug(e["row"].get("company")) in subject_slug]
    if len(by_name) == 1:
        return by_name[0], "company name in subject"
    if len(by_name) > 1:
        return by_name[0], "company name in subject (first of %d rows)" % len(by_name)
    if domain in GENERIC_DOMAINS:
        return None, f"generic domain {domain} and no company name in subject"
    return None, f"no row for {domain or 'unknown sender'}"


def cmd_apply_email(args, log):
    try:
        msgs = load_mail(args.json)
    except (OSError, ValueError) as exc:
        log(f"stages: cannot read --json {args.json}: {exc}")
        return 2
    today = parse_day(args.today) or date.today()
    day = today.isoformat()
    lines, apps = read_apps(args.tracker)

    proposals, unmatched, ambiguities, noops = [], [], [], []
    for msg in msgs:
        stage, why = message_stage(msg)
        subject = norm(msg.get("subject"))
        sender = norm(msg.get("from") or msg.get("from_name"))
        if not stage:
            ambiguities.append({"subject": subject, "from": sender,
                                "reason": why})
            continue
        entry, basis = match_row(apps, sender, subject)
        if entry is None:
            unmatched.append({"subject": subject, "from": sender,
                              "reason": basis, "stage": stage})
            continue
        current = stage_of(entry["row"])
        if current == stage:
            noops.append({"company": norm(entry["row"].get("company")),
                          "role": norm(entry["row"].get("role")),
                          "from": current, "to": stage,
                          "reason": "already at this stage"})
            continue
        errs = transition_errors(current, stage, args.force)
        if errs:
            noops.append({"company": norm(entry["row"].get("company")),
                          "role": norm(entry["row"].get("role")),
                          "from": current, "to": stage, "reason": errs[0]})
            continue
        proposals.append({"entry": entry, "company": norm(entry["row"].get("company")),
                          "role": norm(entry["row"].get("role")),
                          "from": current, "to": stage, "basis": basis,
                          "subject": subject, "from_addr": sender})

    written = 0
    if args.commit and proposals:
        per_record = {}
        for prop in proposals:
            per_record.setdefault(id(prop["entry"]), prop)
        # Bottom-up by line so earlier inserts never shift later indexes.
        ordered = sorted(per_record.values(),
                         key=lambda p: p["entry"]["record"]["start"],
                         reverse=True)
        for prop in ordered:
            rec = prop["entry"]["record"]
            plan, errs = plan_update(lines, rec, prop["to"], day, "email",
                                     prop["subject"], True)
            if errs:
                continue
            edits, inserts = plan
            lines = apply_edits(lines, edits, inserts)
            written += 1
        write_lines(args.tracker, lines)

    payload = {
        "date": day,
        "committed": bool(args.commit),
        "proposed": [{k: v for k, v in p.items() if k != "entry"}
                     for p in proposals],
        "unmatched": unmatched,
        "ambiguous": ambiguities,
        "skipped": noops,
        "written": written,
    }
    if args.json_out:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"{len(proposals)} proposed transition(s)"
          + (f", {written} written" if args.commit else " (dry run)"))
    for prop in proposals:
        print(f"  {prop['company']} · {prop['role']}: "
              f"{prop['from'] or '?'} -> {prop['to']} "
              f"[{prop['basis']}] {prop['subject']}")
    for item in noops:
        print(f"  skip {item['company']} · {item['role']}: "
              f"{item['from']} -> {item['to']} ({item['reason']})")
    if ambiguities:
        print(f"ambiguous {len(ambiguities)} message(s), not guessed:")
        for item in ambiguities:
            print(f"  \"{item['subject']}\" - {item['from']} ({item['reason']})")
    if unmatched:
        print(f"unmatched {len(unmatched)} message(s), not guessed:")
        for item in unmatched:
            print(f"  \"{item['subject']}\" - {item['from']} ({item['reason']})")
    if not args.commit:
        print("dry run: nothing written (pass --commit to write)")
    return 0


# ---------------------------------------------------------------- parser
def add_common(ap):
    ap.add_argument("--tracker", default=TRACKER,
                    help="applications.yaml (default: ./repo/applications.yaml, "
                         "env JOBSEARCH_TRACKER)")


def build_parser():
    ap = argparse.ArgumentParser(
        description="Email-driven stage machine for applications.yaml.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    l = sub.add_parser("list", help="every application's stage and staleness")
    add_common(l)
    l.add_argument("--json", action="store_true")
    l.add_argument("--stage", choices=list(s.ORDER), help="filter by stage")
    l.add_argument("--today", help="date to measure from (YYYY-MM-DD)")

    st = sub.add_parser("set", help="move one application to a stage")
    add_common(st)
    st.add_argument("--company", required=True)
    st.add_argument("--role", help="role, when a company has several rows")
    st.add_argument("--stage", required=True,
                    help="one of: " + ", ".join(s.ORDER) + " (aliases accepted)")
    st.add_argument("--date", required=True, help="stage date (YYYY-MM-DD)")
    st.add_argument("--source", default="manual", choices=SOURCES)
    st.add_argument("--evidence", default="", help="one-line quote or subject")
    st.add_argument("--dry-run", action="store_true")
    st.add_argument("--force", action="store_true",
                    help="allow backwards moves and Rejected rows")
    st.add_argument("--json", action="store_true")

    ae = sub.add_parser("apply-email", help="propose stages from mail JSON")
    add_common(ae)
    ae.add_argument("--json", required=True, help="gmail_browser.py --json file")
    ae.add_argument("--commit", action="store_true",
                    help="write the proposals (default is a dry run)")
    ae.add_argument("--today", help="date to stamp (YYYY-MM-DD)")
    ae.add_argument("--force", action="store_true")
    ae.add_argument("--json-out", action="store_true", help="emit JSON report")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)  # noqa: E731
    if args.cmd == "list":
        return cmd_list(args, log)
    if args.cmd == "set":
        return cmd_set(args, log)
    if args.cmd == "apply-email":
        return cmd_apply_email(args, log)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
