#!/usr/bin/env python3
"""Owner-only job-application action brief (stdlib only).

Purpose: the `jobs` bot carries pipeline status chatter (drain receipts,
enqueue/drain announcements, review verdicts). This script is the opposite:
it prints ONLY items that need the owner, plus hard deadlines, so the
PA chat can carry a single actionable message a day.

Sources:
  queue.yaml            - item statuses and blocker notes
  deadlines.yaml        - curated hard deadlines (local, not in the resume repo)
  outbox/*.md           - drafts waiting for owner review (filename + title)

Buckets:
  NOW      deadlines inside DANGER_DAYS (default 3), or already due
  SOON     deadlines inside LOOKAHEAD_DAYS (default 14)
  YOURS    blocked queue items whose note names an owner action
  STUCK    in_progress claims older than STALE_HOURS (default 3)
  PARKED   non-summer holds, counted only

Silence contract: if every bucket is empty, print nothing and exit 0.
Usage: actions.py [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.environ.get("JOBSEARCH_QUEUE", os.path.join(HERE, "queue.yaml"))
REPO = os.path.join(HERE, "repo")
DEADLINES = os.environ.get("JOBSEARCH_DEADLINES", os.path.join(HERE, "deadlines.yaml"))
OUTBOX = os.environ.get("JOBSEARCH_OUTBOX", os.path.join(HERE, "outbox"))

DANGER_DAYS = 3
LOOKAHEAD_DAYS = 14
STALE_HOURS = 3
MAX_PER_BUCKET = 6

# Owner-action signals in a blocker note. Parked windows are excluded first:
# a non-summer hold is a decision already made, not work outstanding.
PARKED = re.compile(
    r"non-summer|jan[-– ]jun 2027|jan[-– ]jul 2027|jan 2027|jan[-– ]may|"
    r"spring 2027|term[- ]time",
    re.I,
)
OWNER = [
    (re.compile(r"essay|cover letter|personal statement|owner review", re.I), "draft/vet your words"),
    (re.compile(r"captcha|challenge|drag[- ]?puzzle", re.I), "solve a challenge"),
    (re.compile(r"sign[- ]?in|account creation|otp|passcode", re.I), "sign in yourself once"),
    (re.compile(r"upload|dropzone|screenshot|file never registers", re.I), "file upload broke"),
    (re.compile(r"free-text|factual claim|owner answer", re.I), "needs a factual answer"),
]
DEAD = re.compile(r"SUPERSEDED|GHOSTED|WITHDRAWN|DO NOT SEND", re.I)
CLOSED = re.compile(
    r"do not apply|no longer accepting|not currently accepting|duplicate of PR|"
    r"only singaporean|cap spent|slots spent|route outright",
    re.I,
)


def load_yaml(path):
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return None
    try:
        with open(path) as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        return None


def today():
    return dt.date.today()


def parse_day(text):
    if not text:
        return None
    if isinstance(text, dt.datetime):
        return text.date()
    if isinstance(text, dt.date):
        return text
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def deadlines(tdy):
    rows = load_yaml(DEADLINES) or []
    out = []
    for r in rows:
        day = parse_day(r.get("due"))
        if not day:
            continue
        delta = (day - tdy).days
        if delta > LOOKAHEAD_DAYS:
            continue
        out.append(
            {
                "due": day.isoformat(),
                "days": delta,
                "what": str(r.get("what") or ""),
                "where": r.get("where") or "",
            }
        )
    out.sort(key=lambda r: r["days"])
    return out


def queue_items():
    items = load_yaml(QUEUE) or []
    return items if isinstance(items, list) else []


def owner_blocked(items):
    rows = []
    for it in items:
        if it.get("status") != "failed":
            continue
        note = str(it.get("notes") or "")
        if CLOSED.search(note) or PARKED.search(note):
            continue
        for pat, label in OWNER:
            if pat.search(note):
                rows.append(
                    {
                        "pr": it.get("pr_number"),
                        "company": str(it.get("company") or ""),
                        "why": label,
                        "detail": clip(note.split("blocked:")[-1] if "blocked:" in note else note, 90),
                    }
                )
                break
    return rows


def stale_claims(items, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = []
    for it in items:
        if it.get("status") != "in_progress":
            continue
        claimed = it.get("claimed_at")
        age_h = None
        if isinstance(claimed, dt.datetime):
            c = claimed if claimed.tzinfo else claimed.replace(tzinfo=dt.timezone.utc)
            age_h = round((now - c).total_seconds() / 3600, 1)
        rows.append({"pr": it.get("pr_number"), "company": clip(it.get("company"), 26), "hours": age_h})
    return rows


def parked_count(items):
    n = 0
    for it in items:
        if it.get("status") != "failed":
            continue
        note = str(it.get("notes") or "")
        if PARKED.search(note) and not CLOSED.search(note):
            n += 1
    return n


def pending_drafts():
    rows = []
    for path in sorted(glob.glob(os.path.join(OUTBOX, "*.md"))):
        if path.endswith(".bak.md"):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = [next(fh, "") for _ in range(3)]
        except OSError:
            continue
        blob = " ".join(head)
        if DEAD.search(blob):
            continue
        if not re.search(r"DRAFT|NOT SENT|NOT SUBMITTED|awaiting owner|owner vets", blob, re.I):
            continue
        stem = os.path.basename(path)[:-3]
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ")
        title = stem[:1].upper() + stem[1:]
        rows.append({"file": os.path.basename(path), "what": clip(title, 40)})
    return rows


def build():
    tdy = today()
    items = queue_items()
    yours = owner_blocked(items)
    # Acknowledged rows stay acked for the day: 'done N' records a local ack
    # rather than editing the queue (the jobs lane owns that file), so every
    # path that renders - list, refresh, done - must apply the same filter or
    # a refresh would resurrect a row the owner just cleared.
    day = tdy.isoformat()
    acked = (load_ack().get("acked") or {})
    yours = [r for r in yours if "%s#%s" % (day, r["pr"]) not in acked]
    return {
        "date": day,
        "deadlines": deadlines(tdy),
        "yours": yours,
        "stuck": stale_claims(items),
        "parked": parked_count(items),
        "drafts": pending_drafts(),
        "queued": sum(1 for i in items if i.get("status") == "queued"),
    }


def md_esc(text):
    """Escape the markdown-significant set, collapsing newlines first so a
    scraped company name cannot break out of its line or forge a block."""
    s = " ".join(str(text or "").split())
    for ch in "\\`*_{}[]()#+!~|&<>":
        s = s.replace(ch, "\\" + ch)
    return s


def _details(out, header, rows, open_=False):
    """One collapsible section holding every row in that bucket.

    Owner 2026-11-03: the brief clipped each bucket (4 deadlines, 2 names per
    group), so it could not answer 'what is actually outstanding'. Collapsed,
    the message height is unchanged; expanded, nothing is hidden.
    """
    if not rows:
        return
    out.append("")
    # Literal section name plus an integer count, so the summary is never
    # escaped; no scraped text reaches it.
    out.append("<details%s><summary>%s (%d)</summary>"
               % (" open" if open_ else "", header, len(rows)))
    out.append("")
    for block in rows:
        out.extend(block)
        out.append("")
    out.append("</details>")


def render_md(data, tdy):
    """Rich-markdown brief: full lists inside collapsible sections.

    Numbering covers the owner-action rows only, because those are the ones
    that need the owner; '/job done N' therefore acts on a small, stable set.
    """
    out = ["### " + md_esc("Job actions · %s" % tdy.strftime("%d/%m/%Y"))]
    urgent = [d for d in data["deadlines"] if d["days"] <= DANGER_DAYS]
    soon = [d for d in data["deadlines"] if d["days"] > DANGER_DAYS]

    def dline(d):
        tag = ("today" if d["days"] == 0
               else "overdue" if d["days"] < 0 else "%dd" % d["days"])
        # Two trailing spaces are a markdown hard break, so the deadline tag
        # lands on its own dim line instead of running on after the title.
        return ["**%s**  \n_%s_" % (md_esc(d["what"]), md_esc(tag))]

    _details(out, "Act now", [dline(d) for d in urgent], open_=True)
    _details(out, "Coming up", [dline(d) for d in soon])

    yours = data["yours"]
    if yours:
        rows = []
        for i, r in enumerate(yours, 1):
            rows.append(["**%d.** %s  \n_%s · PR %s_" % (
                i, md_esc(r["company"]), md_esc(r["why"]), md_esc(r["pr"]))])
        _details(out, "Yours to do", rows, open_=True)
    else:
        out.append("")
        out.append("Nothing waiting on you.")

    if data["drafts"]:
        rows = [["**%s**" % md_esc(d["what"])] for d in data["drafts"]]
        _details(out, "Drafts to vet", rows, open_=True)

    if data["stuck"]:
        rows = []
        for r in data["stuck"]:
            h = "%sh" % r["hours"] if r["hours"] is not None else "unknown"
            rows.append(["**%s**  \n_in flight %s_" % (md_esc(r["company"]), md_esc(h))])
        _details(out, "Stuck in flight", rows)

    health = []
    if data["queued"]:
        health.append("%d queued" % data["queued"])
    if data["parked"]:
        health.append("%d held (non-summer)" % data["parked"])
    if health:
        out.append("")
        out.append("_Pipeline · %s_" % md_esc(" · ".join(health)))
    if yours:
        out.append("")
        out.append("_/job done <n> to close one · /job list to re-send_") 
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def render(data, md=False):
    """The brief. md=True emits rich markdown for sendRichMessage.

    Owner 2026-11-03: the first version clipped every bucket (4 deadlines, 4
    group lines, 2 names each) and had no way to see the rest, so it could not
    answer 'what is actually outstanding'. Rich mode puts each bucket in a
    native collapsible <details> block carrying its count, lists every row, and
    numbers the owner-action rows so they can be handled from the message.
    """
    tdy = dt.date.fromisoformat(data["date"])
    if not any((data["deadlines"], data["yours"], data["stuck"])):
        return ""
    if md:
        return render_md(data, tdy)
    lines = []
    urgent = [d for d in data["deadlines"] if d["days"] <= DANGER_DAYS]
    soon = [d for d in data["deadlines"] if d["days"] > DANGER_DAYS]

    if urgent:
        lines.append("### Act now")
        for d in urgent[:3]:
            tag = "today" if d["days"] == 0 else ("overdue" if d["days"] < 0 else f"{d['days']}d")
            lines.append(f"- {clip(d['what'], 40)} · {tag}")
        lines.append("")

    if soon:
        lines.append("### Coming up")
        for d in soon[:4]:
            lines.append(f"- {clip(d['what'], 40)} · {d['days']}d")
        lines.append("")

    if data["yours"]:
        lines.append(f"### Yours · {len(data['yours'])}")
        groups = {}
        for r in data["yours"]:
            groups.setdefault(r["why"], []).append(r["company"])
        for why, names in list(groups.items())[:4]:
            uniq = []
            for n in names:
                base = n.split("…")[0].strip()
                if base not in uniq:
                    uniq.append(base)
            shown = ", ".join(clip(n, 19) for n in uniq[:2])
            extra = f" +{len(uniq) - 2}" if len(uniq) > 2 else ""
            lines.append(f"- {why}: {shown}{extra}")
        lines.append("")

    if data["drafts"]:
        names = [d["what"] for d in data["drafts"]]
        lines.append(f"Drafts to vet · {len(names)}: " + ", ".join(clip(n, 22) for n in names[:2]))

    health = []
    if data["queued"]:
        health.append(f"{data['queued']} queued")
    if data["parked"]:
        health.append(f"{data['parked']} held")
    for r in data["stuck"]:
        h = f"{r['hours']}h" if r["hours"] is not None else "unknown"
        health.append(f"{clip(r['company'].split('…')[0], 14)} in flight {h}")
    if health:
        lines.append("Pipeline · " + " · ".join(health))

    while lines and not lines[-1].strip():
        lines.pop()
    return f"Job actions · {tdy.strftime('%d/%m/%Y')}\n\n" + "\n".join(lines)


ACK_FILE = os.path.join(HERE, "actions-state.json")


def load_ack():
    try:
        with open(ACK_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_ack(state):
    tmp = ACK_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, ACK_FILE)


def _post(method, payload, token, timeout=20):
    import urllib.error
    import urllib.request
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError("%s %s: %s" % (method, e.code, detail[:300]))


def _cfg():
    with open(os.path.expanduser("~/.openclaw/openclaw.json")) as fh:
        return json.load(fh)


def _bot_token():
    return _cfg()["channels"]["telegram"]["botToken"]


def _chat_id():
    allow = (_cfg().get("commands") or {}).get("ownerAllowFrom") or []
    for a in allow:
        if str(a).startswith("telegram:"):
            return int(str(a).split(":", 1)[1])
    return 7377889596


def buttons_for(n_yours):
    """Number keys for the owner-action rows plus a refresh.

    Only numbered rows need an action, so the set of tappable numbers is
    exactly the set '/job done N' accepts.
    """
    rows, row = [], []
    for n in range(1, min(n_yours, 8) + 1):
        row.append({"text": str(n), "callback_data": "job:done %d" % n})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "\u21bb Refresh", "callback_data": "job:refresh"}])
    return {"inline_keyboard": rows}


def send(data, mid=None):
    """Send (mid=None) or edit (mid set) the brief as one rich message.

    Rich send must use sendRichMessage; sendMessage rejects a rich_message
    payload with 'message text is empty' (verified 2026-11-03). The edit uses
    editMessageText with a rich_message payload, which the channel keeps rich.
    """
    text = render(data, md=True)
    if not text:
        return None
    token = _bot_token()
    chat = _chat_id()
    kb = buttons_for(len(data["yours"]))
    if mid:
        try:
            _post("editMessageText", {
                "chat_id": chat, "message_id": int(mid),
                "rich_message": {"markdown": text}, "reply_markup": kb,
            }, token)
        except RuntimeError as e:
            if "message is not modified" not in str(e):
                raise
        return int(mid)
    r = _post("sendRichMessage", {
        "chat_id": chat, "rich_message": {"markdown": text},
        "reply_markup": kb,
    }, token)
    if not r.get("ok"):
        raise RuntimeError("sendRichMessage: %s" % str(r)[:300])
    return r["result"]["message_id"]


def cmd_done(data, spec, mid=0):
    """Acknowledge owner-action rows by number and re-render what is left.

    The brief is a prompt, not a tracker: a numbered row is a reminder of real
    pipeline state (a queue item blocked on the owner), so tapping it records
    a local ack for today rather than mutating queue.yaml - the job that owns
    the queue would overwrite an edit anyway, and claiming a PR is done when
    the upload is still broken would be a lie.
    """
    nums = [int(x) for x in re.findall(r"\d+", spec or "")]
    if not nums:
        return 2
    yours = data["yours"]
    picked = []
    for n in nums:
        if 1 <= n <= len(yours):
            picked.append(yours[n - 1])
    if not picked:
        print("job: no such row")
        return 1
    state = load_ack()
    day = data["date"]
    acked = state.setdefault("acked", {})
    for r in picked:
        acked["%s#%s" % (day, r["pr"])] = r["company"]
    save_ack(state)
    # Rebuild so the filter in build() applies to every bucket, not just the
    # list the numbers came from.
    data = build()
    if mid:
        out = send(data, mid=mid)
        print("refreshed" if out else "empty")
    else:
        names = ", ".join(r["company"] for r in picked)
        print("Acked: %s" % names)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="send the brief to the owner chat")
    sub.add_parser("render", help="print the rich markdown, do not send")
    r = sub.add_parser("refresh", help="edit an existing brief in place")
    r.add_argument("mid", type=int)
    d = sub.add_parser("done", help="close owner-action row N")
    d.add_argument("nums")
    d.add_argument("--mid", type=int, default=0)
    args = ap.parse_args()
    data = build()
    if args.cmd == "render":
        print(render(data, md=True))
        return 0
    if args.cmd == "refresh":
        # mid 0 means the callback carried no message id: send a fresh brief
        # rather than trying to edit message 0.
        out = send(data, mid=args.mid or None)
        print("refreshed" if out else "empty")
        return 0
    if args.cmd == "done":
        return cmd_done(data, args.nums, args.mid)
    if args.cmd == "list":
        out = send(data)
        print("sent %s" % out if out else "empty")
        return 0
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    text = render(data)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
