#!/usr/bin/env python3
"""Nightly job-application status digest (stdlib only).

Reads the application tracker (``applications.yaml``) from a **read-only** clone
of your resume repo and, for context, the discovered postings
(``data/postings.json``). It never writes to the repo: the only file it writes is
a small date-based state file (``summary_state.json``) next to this script.

The digest reports, Telegram-ready and in this order:

  (a) applications submitted since the previous digest -- tracker entries whose
      ``applied_date`` falls after the date recorded in the state file;
  (b) counts by status -- ``Found`` / ``Tailored`` / ``Applied`` / ``Interview``
      / ``Offer`` / ``Rejected``. Tailored work lives as open PRs in the resume
      repo (one per tailored CV; the tracker only gains an entry at submission
      time), so ``Tailored`` is tracker entries plus open tailor PRs, and
      postings matched to an open tailor PR leave ``Found``. The remaining
      statuses come from ``applications.yaml``;
  (c) applications ageing without a reply -- ``Applied`` entries whose
      ``applied_date`` is older than ``--days`` (default 7);
  (d) a single short "awaiting action" line when neither (a) nor (c) produced
      anything (nothing changed).

Output is silent (nothing on stdout, exit 0) when there is nothing at all to
say, so a headless cron payload can suppress delivery. Diagnostics go to stderr
only.

Since 2026-10-12 the changed-digest sections render as compact monospace tables
(emoji-aware display widths, ≤50 cols) inside ``` fences, mirroring the
canvas-nudges digest. Since 2026-10-21 the digest header is ``Daily digest ·
DD/MM/YYYY`` (owner Telegram template); the announce filters key on the
template titles (``Daily digest · ``, ``Needs a nudge · ``, ``New postings
· ``).

Usage:
  summary.py [digest] [--repo DIR] [--days N] [--dry-run] [--json]
  summary.py nudges [--repo DIR] [--dry-run] [--json]
  summary.py newpost [--repo DIR] [--skipped PATH] [--rankings PATH] [--dry-run] [--json]

  Bare invocation = digest mode. ``--mode {digest,nudges,newpost}`` is also
  accepted and wins over the positional subcommand.
"""
import argparse
import html as html_mod
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.environ.get("JOBSEARCH_REPO", os.path.join(HERE, "repo"))
STATE_FILE = os.environ.get("JOBSEARCH_SUMMARY_STATE",
                           os.path.join(HERE, "summary_state.json"))

TRACKER_PATH = "applications.yaml"
POSTINGS_PATH = "data/postings.json"
BRANCH_PREFIX = "app/"
GH_REPO_DEFAULT = "example-user/resume"
TAILOR_PR_LIMIT = 200

# Canonical funnel stages, in digest order (email-driven stage machine,
# 2026-10-31). ``Callback`` and ``OA`` are optional: an employer may go from
# ``Applied`` straight to ``Interview``, and nothing may assume the stages
# between two recorded stages happened. ``Offer`` survives only as a tracker
# alias of ``Accepted`` so old rows keep working.
ORDER = ("Found", "Tailored", "Applied", "Callback", "OA", "Interview",
         "Accepted", "Rejected")

# Optional stages: either may be skipped on any given application.
OPTIONAL_STAGES = ("Callback", "OA")

# Stage index for ordering / backsliding checks (``stages.py`` uses it too).
STAGE_INDEX = {name: i for i, name in enumerate(ORDER)}


def stage_index(stage):
    """Rank of a canonical stage in ``ORDER``; unknown -> -1."""
    return STAGE_INDEX.get(stage, -1)


# Statuses that mean the application has actually been submitted, so they are
# eligible for the "new since the previous digest" section. ``Tailored`` /
# ``Found`` are pre-submission and only feed the counts (and awaiting-action).
SUBMITTED_STATUSES = frozenset({"Applied", "Callback", "OA", "Interview",
                                "Accepted", "Rejected"})

# Tracker ``status``/``stage`` aliases -> canonical stage. The tracker header
# documents ``to_apply``/``applied``/``interviewing``/``offer``/``rejected``/
# ``withdrawn``; the rest arrive from recruiter mail classifications.
STATUS_ALIASES = {
    "found": "Found", "to_apply": "Found", "todo": "Found", "not_applied": "Found",
    "tailored": "Tailored", "tailor": "Tailored", "ready": "Tailored",
    "applied": "Applied", "submitted": "Applied",
    "callback": "Callback", "recruiter": "Callback", "info_requested": "Callback",
    "rfi": "Callback", "screening": "Callback", "screen": "Callback",
    "oa": "OA", "assessment": "OA", "online_assessment": "OA",
    "hackerrank": "OA", "codility": "OA", "test": "OA", "challenge": "OA",
    "interview": "Interview", "interviewing": "Interview", "interviewed": "Interview",
    "offer": "Accepted", "offered": "Accepted", "accepted": "Accepted",
    "signed": "Accepted",
    "rejected": "Rejected", "rejection": "Rejected",
    "withdrawn": "Rejected", "declined": "Rejected",
}

# Asia/Singapore (UTC+8); zoneinfo is stdlib on 3.9+, with a fixed-offset fallback.
try:
    from zoneinfo import ZoneInfo
    _SGT = ZoneInfo("Asia/Singapore")
except Exception:  # pragma: no cover - tz database unavailable
    _SGT = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- yaml subset
# Minimal, tracker-only YAML reader. Deliberately the same subset accepted by
# tailor.py/discover.py: a top-level list of scalar mappings, plus ``[]``.
def _strip_comment(line):
    out, quote = [], None
    for ch in line:
        if ch in "\"'":
            quote = None if quote == ch else (ch if quote is None else quote)
        if ch == "#" and quote is None:
            break
        out.append(ch)
    return "".join(out)


def _unq(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _scalar(v):
    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "~", ""):
        return None
    return _unq(v)


def parse_yaml_list(text):
    """Minimal YAML subset: a top-level list of mappings (applications.yaml)."""
    items, cur = [], None
    for raw in text.splitlines():
        s = _strip_comment(raw).strip()
        if not s or s in ("---", "...", "[]"):
            continue
        if s.startswith("- "):
            cur = {}
            items.append(cur)
            s = s[2:].strip()
            if s and ":" in s:
                k, _, v = s.partition(":")
                cur[k.strip()] = _scalar(v)
        elif cur is not None and ":" in s:
            k, _, v = s.partition(":")
            cur[k.strip()] = _scalar(v)
    return items


# ---------------------------------------------------------------- helpers
def norm_text(s):
    return re.sub(r"\s+", " ", html_mod.unescape(str(s or ""))).strip()


def normalize_url(url):
    import urllib.parse
    p = urllib.parse.urlsplit(str(url or "").strip())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def parse_date(value):
    """Parse an ISO ``YYYY-MM-DD`` (or datetime) string to a date, else None."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def canonical_stage(value):
    """Map a stage/status string to one of ``ORDER``, or None if unknown."""
    key = re.sub(r"[\s-]+", "_", norm_text(value).lower())
    return STATUS_ALIASES.get(key)


def row_stage(row):
    """Stage of a tracker row: ``stage`` wins over ``status`` when present.

    Stage provenance lives in optional keys on the row (``stage``,
    ``stage_since``, ``stage_source``, ``stage_evidence``) and is written by
    ``stages.py``; a row without them behaves exactly as before.
    """
    if not isinstance(row, dict):
        return None
    for field in ("stage", "status"):
        if norm_text(row.get(field)):
            got = canonical_stage(row.get(field))
            if got:
                return got
    return None


def canonical_status(status):
    """Map a tracker status string to one of ``ORDER``, or None if unknown."""
    return canonical_stage(status)


def tracker_keys(apps):
    """Normalised link + company|role keys that count as already tracked."""
    links, names = set(), set()
    for a in apps:
        if not isinstance(a, dict):
            continue
        link = normalize_url(a.get("link", "")).lower()
        if link:
            links.add(link)
        company = norm_text(a.get("company", "")).lower()
        role = norm_text(a.get("role", "")).lower()
        if company or role:
            names.add(f"{company}|{role}")
    return links, names


def is_tracked(posting, links, names):
    link = normalize_url(posting.get("link", "")).lower()
    if link and link in links:
        return True
    company = norm_text(posting.get("company", "")).lower()
    role = norm_text(posting.get("role", "")).lower()
    return f"{company}|{role}" in names


# --------------------------------------------------------------- tailor PRs
# Tailored CVs ship as open PRs in the resume repo (head branch ``app/*``,
# title ``app(<slug>): tailored ...`` per TAILOR_BRIEF.md). The digest counts
# them as ``Tailored`` and keeps their postings out of ``Found``. Everything
# here is best-effort: when gh is unavailable the digest falls back to
# tracker-only counts (same failure style as tailor.py's PR check).
def is_tailor_pr(pr):
    """True for a tailor PR: ``app/*`` head branch + ``: tailored `` title."""
    head = str((pr or {}).get("headRefName") or "")
    title = str((pr or {}).get("title") or "")
    return head.startswith(BRANCH_PREFIX) and ": tailored " in title


def fetch_open_tailor_prs(log=lambda *a: None):
    """Open tailor PRs as dicts, or ``None`` when the check is unavailable."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo",
             os.environ.get("JOBSEARCH_GH_REPO", GH_REPO_DEFAULT),
             "--state", "open", "--json", "number,headRefName,title,body",
             "--limit", str(TAILOR_PR_LIMIT)],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 - gh/network failures are expected
        log(f"summary: PR check failed: {str(exc).replace(chr(10), ' | ')}")
        return None
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or str(proc.returncode)).replace("\n", " | ")
        log(f"summary: PR check failed: {detail}")
        return None
    try:
        prs = json.loads(proc.stdout or "[]")
    except ValueError:
        log("summary: PR check returned invalid JSON")
        return None
    return [p for p in prs if isinstance(p, dict) and is_tailor_pr(p)]


def slugify(text, maxlen=60):
    """Same rule as tailor.py: lowercase, non-alphanumerics to single dashes."""
    text = html_mod.unescape(str(text or "")).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return (text[:maxlen].rstrip("-")) or "item"


def work_slug(posting):
    """tailor.py's derived work slug: ``<company-slug>-<role-slug>``."""
    return f"{slugify(posting.get('company', ''))}-{slugify(posting.get('role', ''))}"


def pr_body_links(pr):
    """Normalised bare URLs found in the PR body (the posting link lives there)."""
    out = set()
    for m in re.finditer(r"https?://[^\s)\]<>\"']+", str((pr or {}).get("body") or "")):
        out.add(normalize_url(m.group(0)).lower())
    return out


def pr_branch_slug(pr):
    """Branch name without the ``app/`` prefix."""
    return str((pr or {}).get("headRefName") or "")[len(BRANCH_PREFIX):]


def posting_pr_matched(posting, pr_links, pr_slugs):
    """True when an open tailor PR covers this posting (body link or work slug)."""
    link = normalize_url(posting.get("link", "")).lower()
    if link and link in pr_links:
        return True
    return work_slug(posting) in pr_slugs


def tracker_pr_numbers(apps):
    """PR numbers referenced by tracker entries' ``pr`` field (URL or number)."""
    nums = set()
    for a in apps:
        if not isinstance(a, dict) or a.get("pr") is None:
            continue
        m = re.search(r"(\d+)", str(a["pr"]))
        if m:
            nums.add(int(m.group(1)))
    return nums


def effective_tailor_prs(prs, apps):
    """Drop PRs already accounted for by the tracker.

    A PR is skipped when a tracker entry references its number in ``pr``, or
    when it covers an already-tracked posting (shared body link or identical
    work slug), so the same tailored CV never counts twice.
    """
    nums = tracker_pr_numbers(apps)
    links, _names = tracker_keys(apps)
    slugs = {work_slug(a) for a in apps if isinstance(a, dict)}
    out = []
    for p in prs:
        if p.get("number") in nums:
            continue
        if pr_body_links(p) & links:
            continue
        if pr_branch_slug(p) in slugs:
            continue
        out.append(p)
    return out


def sgt_today(now=None):
    if TODAY is not None:
        return TODAY
    return (now or datetime.now(_SGT)).date()


def read_state(path):
    """Return the last digest date, or None when absent/unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return parse_date(data.get("last_digest")) if isinstance(data, dict) else None


def write_state(path, digest_date, stamp):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"last_digest": digest_date.isoformat(), "updated": stamp},
                  fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_inputs(repo_dir, log=lambda *a: None):
    """Read tracker + postings from the (read-only) resume clone."""
    tracker_path = os.path.join(repo_dir, TRACKER_PATH)
    postings_path = os.path.join(repo_dir, POSTINGS_PATH)
    apps = []
    if os.path.isfile(tracker_path):
        with open(tracker_path, encoding="utf-8") as fh:
            apps = parse_yaml_list(fh.read())
    else:
        log(f"summary: no tracker at {tracker_path}")
    postings = []
    if os.path.isfile(postings_path):
        try:
            with open(postings_path, encoding="utf-8") as fh:
                postings = json.load(fh)
        except ValueError as exc:
            log(f"summary: postings.json unreadable ({exc})")
    else:
        log(f"summary: no postings at {postings_path}")
    return apps, postings


# ---------------------------------------------------------------- core
def compute(apps, postings, last_digest, today, days, prs=None):
    """Pure digest computation. ``last_digest`` may be None (no baseline).

    ``prs`` carries ``fetch_open_tailor_prs`` output: open tailor PRs add to
    ``Tailored`` and keep their postings out of ``Found``. ``None`` (gh
    unavailable) falls back to tracker-only counts.
    """
    apps = [a for a in apps if isinstance(a, dict)]
    counts = {k: 0 for k in ORDER}
    for a in apps:
        bucket = row_stage(a)
        if bucket:
            counts[bucket] += 1

    links, names = tracker_keys(apps)
    tailor_prs = effective_tailor_prs(prs, apps) if prs is not None else []
    counts["Tailored"] += len(tailor_prs)
    pr_links, pr_slugs = set(), set()
    for p in tailor_prs:
        pr_links |= pr_body_links(p)
        pr_slugs.add(pr_branch_slug(p))
    found = [p for p in postings
             if isinstance(p, dict) and not is_tracked(p, links, names)
             and (prs is None or not posting_pr_matched(p, pr_links, pr_slugs))]
    counts["Found"] += len(found)

    new = []
    for a in apps:
        if row_stage(a) not in SUBMITTED_STATUSES:
            continue
        d = parse_date(a.get("applied_date"))
        if d and last_digest and last_digest < d <= today:
            new.append(a)
    new.sort(key=lambda a: parse_date(a.get("applied_date")) or today)

    ageing = []
    for a in apps:
        if row_stage(a) != "Applied":
            continue
        d = parse_date(a.get("applied_date"))
        if d and (today - d).days > days:
            ageing.append((a, (today - d).days))
    ageing.sort(key=lambda pair: pair[1], reverse=True)

    changed = bool(new) or bool(ageing)
    total = sum(counts.values())
    n_found = 0
    if last_digest is not None:
        for p in postings:
            if not isinstance(p, dict):
                continue
            d = parse_date(p.get("added")) or parse_date(p.get("posted"))
            if d and last_digest < d <= today:
                n_found += 1
    return {
        "date": today.isoformat(),
        "last_digest": last_digest.isoformat() if last_digest else None,
        "days": days,
        "counts": counts,
        "new": new,
        "n_found": n_found,
        "ageing": ageing,
        "changed": changed,
        "silent": total == 0,
        "awaiting_action": {
            "tailored_to_submit": counts["Tailored"],
            "found_to_tailor": counts["Found"],
        },
    }


def _counts_line(counts):
    return " · ".join(f"{k} {counts[k]}" for k in ORDER)


# ------------------------------------------------- shared render helpers
# Used by all three Telegram modes (digest / nudges / newpost). Module stays
# importable with no side effects; tests import these directly.
# Module-level today override for deterministic tests: when set, sgt_today()
# returns it instead of the real date.
TODAY = None

# Whole-message line budget (telegram-template.md: under 15 lines).
# The ageing groups replaced the old 40-row monospace tables; ``_pack``
# allocates these lines and guarantees the cap. Option A folds each
# truncation notice into its own section heading, so no line is spent on a
# separate ``+N more`` tail.
DIGEST_MAX_LINES = 14
NUDGE_MAX_LINES = 15
NEWPOST_MAX_LINES = 13
# Telegram rejects a message whose rich entities do not parse; the cron
# wrapper also slices to 3500 chars blindly. Staying under this guard keeps
# any chop clear of an ``<a href>`` tag.
MESSAGE_CHAR_GUARD = 3450
# Sender identity line: <agent> · <lane> · <date>. The agent segment names the
# automation that produced the message: the digest and nudges lanes run under
# the ``jobs`` agent, the newpost lane under ``job-scout``.
PREFACE_AGENT = {
    "summary": "jobs",
    "nudges": "jobs",
    "newpost": "job-scout",
}


def fmt_ddmmyyyy(d):
    """Date -> ``DD/MM/YYYY`` (template global rule); unparseable -> ''."""
    d = parse_date(d)
    return d.strftime("%d/%m/%Y") if d else ""


def fmt_ddmm(d):
    """Date -> ``DD/MM`` for inline nudge/posting lines."""
    d = parse_date(d)
    return d.strftime("%d/%m") if d else ""


def age_days(day, today):
    """Whole days from ``day`` to ``today`` (None-safe -> None)."""
    d = parse_date(day)
    return (today - d).days if d else None


def html_anchor(link, label):
    """Telegram HTML link; company names are the only links in a message."""
    url = html_mod.escape(normalize_url(label and link or link), quote=True)
    return f'<a href="{url}">{html_mod.escape(_dash(label))}</a>'


def _dash(text):
    """Normalise em/en dashes out of emitted text (owner directive).

    A spaced dash becomes ``·`` (the notification separator); an unspaced one
    becomes ``-`` so ranges such as ``May–Aug`` survive as ``May-Aug``.
    Hyphens inside compound words are untouched."""
    text = norm_text(text)
    text = re.sub(r"\s*[—–]\s*",
                  lambda m: " · " if " " in m.group(0) else "-", text)
    return norm_text(text)


def esc(text):
    """HTML-escape body text (roles, terms) for Telegram HTML messages.

    Dashes are normalised first: the owner's standing directive forbids em
    and en dashes in anything written for him (see ``_dash``)."""
    return html_mod.escape(_dash(text), quote=False)


def _preface(agent, lane, today):
    """Sender/lane identity line shared by all three messages.

    ``jobs · summary · 26/10/2026`` tells the owner which automation (and
    which bot account) produced the message at a glance. ``today`` may be a
    date or an ISO string; an unknown date renders as an empty tail."""
    return f"{agent} · {lane} · {fmt_ddmmyyyy(today)}"


def _plural(n, word):
    """``1 application`` / ``8 applications``."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _heading(title, total=None, meta=""):
    """Section heading factory: ``### Title · N · meta · +K not shown``.

    Returns a callable ``(shown, hidden) -> str`` for ``_pack``. The count is
    the whole group (so it never changes when the list is trimmed) and
    ``+K not shown`` is the number of entries the line budget dropped; the
    clause disappears when nothing is hidden. ``total`` may be ``None`` for
    sections whose size is carried by ``meta`` instead of a plain count.
    Title and meta go through ``_dash`` so no em or en dash can reach the
    wire."""
    def render(shown, hidden):
        parts = [f"### {_dash(title)}"]
        if total is not None:
            parts.append(str(total))
        if meta:
            parts.append(_dash(meta))
        if hidden:
            parts.append(f"+{hidden} not shown")
        return " · ".join(parts)
    return render


def _emit(lines):
    """Join, then degrade by whole lines until the character guard holds.

    A blind slice can cut through an ``<a href="...">`` and Telegram rejects
    the whole message, so oversized output loses whole lines from the bottom
    of the body instead, never the two identity lines at the top and never
    the closing line. Dropping whole lines keeps every tag balanced."""
    lines = list(lines)
    while len("\n".join(lines)) > MESSAGE_CHAR_GUARD and len(lines) > 4:
        # drop the longest body line: sheds the most characters per pass and
        # leaves as many entries standing as the budget allows
        body = range(2, len(lines) - 1)
        del lines[max(body, key=lambda i: len(lines[i]))]
    out = "\n".join(lines)
    assert len(out) <= MESSAGE_CHAR_GUARD, len(out)
    return out


def _pack(groups, budget):
    """Lay out grouped sections within ``budget`` lines, in priority order.

    Each group is ``{header, entries, max, floor, fmt}``. ``header`` is a
    callable ``(shown, hidden) -> str`` so the truncation notice folds into
    the heading itself (``### Chase now · 19 · 14d or more · +15 not shown``)
    instead of costing a separate tail line. ``fmt`` receives ``(n, item)``
    where ``n`` is the running global entry number, so numbering stays
    contiguous across sections even after truncation (the ``/chase <n>`` /
    ``/snooze <n>`` contract). Callers pass groups in priority order.

    Pass 1 gives every group its ``floor`` (so no section silently vanishes),
    pass 2 tops groups up front-first, i.e. the highest-priority section takes
    spare lines first. A group that cannot fit even one entry is dropped whole
    rather than shown as an empty header. Never exceeds ``budget``.
    """
    live = [g for g in groups if g.get("entries")]
    caps = [0] * len(live)

    def total(cs):
        return sum(1 + c for c in cs if c)

    def grow(i, ceiling):
        while caps[i] < ceiling:
            cand = caps[:]
            cand[i] += 1
            if total(cand) > budget:
                break
            caps[i] += 1

    for i, g in enumerate(live):
        grow(i, min(len(g["entries"]), g.get("floor", 1)))
    for i, g in enumerate(live):
        grow(i, min(len(g["entries"]), g.get("max", 5)))
    out, n = [], 0
    for g, c in zip(live, caps):
        if not c:
            continue
        out.append(g["header"](c, len(g["entries"]) - c))
        for item in g["entries"][:c]:
            n += 1
            out.append(g["fmt"](n, item) if g.get("fmt") else item)
    return out


def cap_lines(lines, limit=15, max_items=5):
    """Cap a message: at most ``max_items`` numbered entries, then
    ``and N more.``; total message stays under ``limit`` lines."""
    return lines  # entries are capped by callers; hook kept for tests


def _fmt_date(iso):
    """Compact table date: ``12 Oct`` (SGT); falls back to the raw value."""
    d = parse_date(iso)
    return f"{d.day} {d.strftime('%b')}" if d else norm_text(iso)[:9]


def _dlen(s):
    """Terminal display width: wide (East-Asian/emoji) code points count 2."""
    return sum(2 for ch in str(s) if unicodedata.east_asian_width(ch) in "WF") + \
           sum(1 for ch in str(s) if unicodedata.east_asian_width(ch) not in "WF")


def _fit(text, width):
    """Word-boundary truncation to display width (ellipsis when cut)."""
    text = norm_text(text)
    if _dlen(text) <= width:
        return text
    # Trim by display width, then re-cut at a word boundary.
    out = ""
    for ch in text:
        if _dlen(out) + _dlen(ch) > width - 1:
            break
        out += ch
    if " " in out:
        out = out.rsplit(" ", 1)[0]
    return out.rstrip(" ,.;:-()") + "…"


def _short_role(role):
    """Display-cleanup: drop bracketed prefixes and Intern/Internship tokens."""
    role = norm_text(role)
    role = re.sub(r"\[[^\]]*\]", " ", role)
    role = re.sub(r"\([^)]*\)", " ", role)
    role = re.sub(r"\b(internship|intern|program|programme)\b", " ", role,
                  flags=re.IGNORECASE)
    role = re.sub(r"\s+,", ",", role)
    return norm_text(role).strip(" -–—:;,.")


def _wrap(text, width):
    """Word-wrap by display width; returns a list of chunks (>=1)."""
    lines, cur = [], ""
    for w in norm_text(text).split(" "):
        cand = (cur + " " + w).strip()
        if _dlen(cand) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        while _dlen(ln) > width:  # hard-break pathological tokens
            out.append(ln[:width])
            ln = ln[width:]
        out.append(ln)
    return out


def _table(header, rows, aligns, total_width=50, wrap_last=False):
    """Fixed-width table: dynamic columns, ``aligns`` l/r per column.

    Widths are display widths (emoji-aware). Squeezes to ``total_width``
    (phone-safe, mirrors the canvas-nudges digest) by capping the widest
    flexible text columns first. ``wrap_last`` word-wraps the final column
    to the remaining width with hanging-indent continuation rows instead
    of truncating it.
    """
    n = len(header)
    rows = [list(r) for r in rows]
    orig = [list(r) for r in rows]
    widths = [max(_dlen(header[i]), *(_dlen(r[i]) for r in rows)) for i in range(n)]
    fixed = sum(w for w, a in zip(widths, aligns) if a == "r") + 2 * (n - 1)
    flex = [i for i, a in enumerate(aligns) if a == "l"]
    # Columns that may be word-wrapped instead of truncated.
    wrapset = {n - 1} if (wrap_last and n >= 2) else set()
    # Cap flexible columns (all but first are flex text columns) to fit.
    over = sum(widths[i] for i in flex) - (total_width - fixed)
    if over > 0 and flex:
        # Trim the widest flex columns first, never below 6.
        for i in sorted(flex, key=lambda i: -widths[i]):
            if over <= 0:
                break
            if i in wrapset:
                continue  # last column wraps instead
            floor = 14 if len(flex) > 1 else 6
            take = min(max(widths[i] - floor, 0), over)
            widths[i] -= take
            over -= take
        # Residual overflow: without a wrap column, shave non-wrap columns
        # to their 6-char floor; with one, the wrap column absorbs it.
        if not wrapset:
            for i in sorted(flex, key=lambda i: -widths[i]):
                if over <= 0:
                    break
                if i in wrapset:
                    continue
                take = min(max(widths[i] - 6, 0), over)
                widths[i] -= take
                over -= take
        for i in flex:
            if i in wrapset:
                continue
            for r in rows:
                if _dlen(r[i]) > widths[i]:
                    r[i] = _fit(r[i], widths[i])
        widths = [max(_dlen(header[i]), *(_dlen(r[i]) for r in rows)) for i in range(n)]
    if wrapset:
        last = n - 1
        last_w = max(12, total_width - (sum(widths[:last]) + 2 * (n - 1)))
        widths[last] = last_w
        expanded = []
        for r in rows:
            chunks = _wrap(r[last], last_w) or [""]
            for k, ch in enumerate(chunks):
                expanded.append(([""] * last + [ch]) if k else list(r[:last]) + [ch])
        rows = expanded
    def pad(v, w):
        return v + " " * (w - _dlen(v))
    def cell(v, w, a):
        return " " * (w - _dlen(v)) + v if a == "r" else pad(v, w)
    lines = [" ".join(cell(h, w, a) for h, w, a in zip(header, widths, aligns))]
    lines += [" ".join(cell(v, w, a) for v, w, a in zip(row, widths, aligns))
              for row in rows]
    return "\n".join(lines)


def _block(content):
    lines = [content] if isinstance(content, str) else list(content)
    return ["```", *lines, "```"]


def render_text(res):
    """Message 1 - daily digest per telegram-template.md (Option A layout).

    Line 1 is the shared sender/lane preface, line 2 the human title. Body is
    hierarchical: a ``###`` section heading carrying its own count, age window
    and ``+N not shown`` notice, then one line per application with the
    company bold and only the age after it. Counts and the day-over-day
    change share one line; the two headline totals close the message. Silent
    when empty. Keeps the ``New since`` / ``no changes`` markers the gate
    relies on.
    """
    if res["silent"]:
        return ""
    counts = res["counts"]
    lines = [_preface(PREFACE_AGENT["summary"], "summary", res["date"]),
             f"Daily digest · {fmt_ddmmyyyy(res['date'])}"]
    n_applied = sum(1 for a in res["new"]
                    if row_stage(a) == "Applied")
    n_replies = sum(1 for a in res["new"]
                    if row_stage(a) in
                    ("Interview", "Accepted", "Rejected"))
    change = (f"+{n_applied} applied, +{res['n_found']} found, "
              f"{n_replies} replies")
    lines.append(
        f"Applied {counts['Applied']} · Callbacks {counts['Callback']} · "
        f"OA {counts['OA']} · Interviews {counts['Interview']} · "
        f"Offers {counts['Accepted']} · Rejections {counts['Rejected']} · "
        f"{change}")
    if not res["changed"]:
        aw = res["awaiting_action"]
        lines.append(f"no changes · awaiting action: "
                     f"{aw['tailored_to_submit']} tailored to submit, "
                     f"{aw['found_to_tailor']} found to tailor")
        return _emit(lines)
    # Priority order (front-filled by the budget): what needs action first,
    # then what is merely waiting, then what arrived today.
    groups = []
    chase = [p for p in res["ageing"] if p[1] >= NUDGE_THRESHOLD]
    wait = [p for p in res["ageing"] if p[1] < NUDGE_THRESHOLD]
    if chase:
        groups.append({
            "header": _heading("Chase now", len(chase),
                               f"{NUDGE_THRESHOLD}d or more"),
            "entries": [_app_line(a, age) for a, age in chase],
            "max": 5, "floor": 2})
    if wait:
        groups.append({
            "header": _heading("Waiting on reply", len(wait),
                               f"under {NUDGE_THRESHOLD}d"),
            "entries": [_app_line(a, age) for a, age in wait],
            "max": 3, "floor": 2})
    if res["new"]:
        last = fmt_ddmmyyyy(res["last_digest"]) or norm_text(res["last_digest"])
        groups.append({
            "header": _heading(f"New since {last}", len(res["new"])),
            "entries": [_app_line(a) for a in res["new"]],
            "max": 3, "floor": 1})
    lines += _pack(groups, DIGEST_MAX_LINES - 4)
    lines.append(f"Found {counts['Found']} · Tailored {counts['Tailored']}")
    return _emit(lines)


def _app_line(a, age=None):
    """One entry: ``**Company** · full role · 22d``.

    The company anchor is wrapped in the bold so it stays a single bold link
    node; the anchor is the only link in the message. Roles are never
    truncated here (the old table ellipsised them)."""
    name = norm_text(a.get("company", "?")) or "?"
    link = norm_text(a.get("link", ""))
    who = html_anchor(link, name) if link else esc(name)
    bits = [f"**{who}**", esc(a.get("role", "?") or "?")]
    if age is not None:
        bits.append(f"{age}d")
    return " · ".join(bits)


def render_json(res):
    def entry(a):
        return {
            "company": a.get("company"),
            "role": a.get("role"),
            "link": a.get("link"),
            "status": row_stage(a),
            "raw_status": a.get("status"),
            "raw_stage": a.get("stage"),
            "stage_since": a.get("stage_since"),
            "stage_source": a.get("stage_source"),
            "stage_evidence": a.get("stage_evidence"),
            "applied_date": a.get("applied_date"),
        }

    return json.dumps({
        "date": res["date"],
        "last_digest": res["last_digest"],
        "days": res["days"],
        "counts": res["counts"],
        "new": [entry(a) for a in res["new"]],
        "n_found": res.get("n_found", 0),
        "ageing": [dict(entry(a), age_days=age) for a, age in res["ageing"]],
        "awaiting_action": res["awaiting_action"],
        "changed": res["changed"],
        "silent": res["silent"],
    }, indent=2, ensure_ascii=False)


# ------------------------------------------------------- nudges (msg 2)
NUDGE_TRAILER = ("Reply /chase <n> for a chase email draft, "
                 "/snooze <n> to snooze 3 days.")
NUDGE_THRESHOLD = 14
NUDGE_CAP = 5


def _row_anchor_date(a, stage):
    """Date the *current* stage started, per stage.

    ``stage_since`` (written by ``stages.py``) wins whenever present; the
    pre-submission stages fall back to ``applied_date`` so rows that predate
    the stage machine keep behaving exactly as before."""
    since = parse_date(a.get("stage_since"))
    if since:
        return since
    if stage in ("Found", "Tailored", "Applied"):
        return parse_date(a.get("applied_date"))
    return None


# Per-stage nudge policy (owner decision, 2026-10-31). The value is the age
# in the current stage at which it needs action; ``None`` = never nudge.
#   Callback   a recruiter asked for something: act within 2 days, urgent now
#   OA         before the assessment deadline when the mail carried one, else
#              5 days; deadline inside 48h is urgent
#   Interview  date known and past: chase the outcome after 3 days; upcoming:
#              quiet unless within 2 days (prep nudge)
#   Applied    unchanged 14-day chase
#   Accepted   needs a decision now, timeboxed to the offer deadline
#   Tailored   no submission after 3 days: submit or drop
#   Found      older than 7 days with no tailor PR: tailor or drop
#   Rejected   never
NUDGE_POLICY = {
    "Callback": 2,
    "OA": 5,
    "Interview": 3,
    "Applied": NUDGE_THRESHOLD,
    "Accepted": 0,
    "Tailored": 3,
    "Found": 7,
    "Rejected": None,
}

# Cross-stage message order (lower = earlier in the message).
STAGE_PRIORITY = {
    "Callback": 0,
    "OA": 1,
    "Accepted": 2,
    "Interview": 3,
    "Applied": 4,
    "Tailored": 5,
    "Found": 6,
    "Rejected": 99,
}

# Short stage label shown on a non-``Applied`` entry line.
STAGE_LABEL = {
    "Callback": "recruiter waiting",
    "OA": "assessment",
    "Interview": "interview",
    "Accepted": "offer",
    "Tailored": "not submitted",
    "Found": "not tailored",
}


def _nudge_reason(a, stage, today):
    """Per-stage policy decision: ``{days, urgent, reason}`` or None.

    ``days`` is the number the entry line shows, ``urgent`` promotes the
    entry into the leading "Act now" section, ``reason`` is the heading-level
    phrase. Returns None when the stage does not need action yet.
    """
    if stage not in NUDGE_POLICY or NUDGE_POLICY[stage] is None:
        return None
    since = _row_anchor_date(a, stage)
    if not since:
        return None
    days = (today - since).days
    threshold = NUDGE_POLICY[stage]

    if stage == "Callback":
        # Top priority bucket: a recruiter is waiting for information.
        return {"days": max(days, 0), "urgent": True,
                "reason": "recruiter waiting"}

    if stage == "OA":
        due = parse_date(a.get("oa_deadline"))
        if due:
            if due < today:
                return {"days": (today - due).days, "urgent": True,
                        "reason": "assessment overdue"}
            left = (due - today).days
            if left <= 2:
                return {"days": days, "urgent": True,
                        "reason": f"assessment due {fmt_ddmm(due)}"}
            if days < threshold:
                return None
            return {"days": days, "urgent": False,
                    "reason": f"assessment due {fmt_ddmm(due)}"}
        if days < threshold:
            return None
        return {"days": days, "urgent": False, "reason": "assessment pending"}

    if stage == "Interview":
        when = parse_date(a.get("interview_date"))
        if when:
            if when >= today:
                # Upcoming interview: quiet until two days out (prep nudge).
                if (when - today).days > 2:
                    return None
                return {"days": days, "urgent": True,
                        "reason": f"interview on {fmt_ddmm(when)}"}
            waited = (today - when).days
            if waited < threshold:
                return None
            return {"days": waited, "urgent": False,
                    "reason": "outcome due"}
        if days < threshold:
            return None
        return {"days": days, "urgent": False, "reason": "chase outcome"}

    if stage == "Accepted":
        due = parse_date(a.get("offer_deadline"))
        if due and due < today:
            return {"days": (today - due).days, "urgent": True,
                    "reason": "offer lapsed"}
        if due:
            left = (due - today).days
            return {"days": max(days, 0), "urgent": left <= 2,
                    "reason": f"decide by {fmt_ddmm(due)}"}
        return {"days": max(days, 0), "urgent": True,
                "reason": "decision due"}

    # Applied keeps its unchanged 14-day chase: every row past day 0 is
    # listed, and the 2-week mark flips it from "chase this week" to
    # "chase now". Tailored (3 days) and Found (7 days) only surface once
    # the stage has actually stalled.
    if stage == "Applied":
        if days < 1:
            return None
        gap = threshold - days
        gap_txt = "1 day" if gap == 1 else f"{gap} days"
        # ``urgent`` is reserved for the exceptional stages; Applied ages
        # into the Chase now / Chase this week sections instead.
        return {"days": days, "urgent": False,
                "reason": "chase now" if days >= threshold
                else f"chase in {gap_txt}"}
    if days < max(threshold, 1):
        return None
    return {"days": days, "urgent": False,
            "reason": STAGE_LABEL.get(stage, "waiting")}


def compute_nudges(apps, today):
    """Stage-driven nudge list; roles join with `` / ``, earliest anchor wins.

    Every row is judged against ``NUDGE_POLICY`` for its current stage (a
    ``stage`` key overrides ``status``; see ``row_stage``). Rows without an
    anchor date or ``link`` are skipped silently, as before. Returns
    ``(entries, silent)``.
    """
    groups, order = {}, []
    for a in apps:
        if not isinstance(a, dict):
            continue
        stage = row_stage(a)
        if not stage:
            continue
        decision = _nudge_reason(a, stage, today)
        if not decision:
            continue
        link = norm_text(a.get("link", ""))
        if not link:
            continue
        since = _row_anchor_date(a, stage)
        key = norm_text(a.get("company", "?")).lower()
        role = norm_text(a.get("role", "")) or "\u2014"
        if key not in groups:
            groups[key] = {"company": norm_text(a.get("company", "?")),
                           "roles": [], "seen": set(), "link": link,
                           "date": since, "stage": stage,
                           "days": decision["days"],
                           "urgent": decision["urgent"],
                           "reason": decision["reason"],
                           "evidence": ""}
            order.append(key)
        g = groups[key]
        if role not in g["seen"]:
            g["seen"].add(role)
            g["roles"].append(role)
        if g["date"] is None or since < g["date"]:
            g["date"] = since
            g["link"] = link
        if STAGE_PRIORITY[stage] < STAGE_PRIORITY[g["stage"]]:
            g["stage"] = stage
            g["days"], g["urgent"] = decision["days"], decision["urgent"]
            g["reason"] = decision["reason"]
        if not g["evidence"]:
            g["evidence"] = norm_text(a.get("stage_evidence", ""))
    entries = []
    for key in order:
        g = groups[key]
        entries.append({"company": g["company"],
                        "roles": list(g["roles"]), "link": g["link"],
                        "date": g["date"], "days": g["days"],
                        "stage": g["stage"], "urgent": g["urgent"],
                        "reason": g["reason"], "evidence": g["evidence"],
                        "action": g["reason"]})
    entries.sort(key=lambda e: (not e["urgent"],
                                STAGE_PRIORITY[e["stage"]],
                                -e["days"], e["company"].lower()))
    return entries, not entries


def render_nudges(entries, today):
    """Message 2 - stage-driven nudge list, Option A layout. Silent when empty.

    Sections, in priority order: ``Act now`` (urgent: recruiter waiting,
    assessment deadline inside 48h, offer decision, interview prep), then
    ``Chase now`` / ``Chase this week`` for ``Applied``, then one section per
    remaining stage. The heading names the stage action, so entry lines stay
    ``n. **Company** · roles · Nd``. Numbering is globally sequential because
    ``/chase <n>`` and ``/snooze <n>`` resolve against it; each section is
    capped at ``NUDGE_CAP`` with its count folded into the heading.
    """
    if not entries:
        return ""
    lines = [_preface(PREFACE_AGENT["nudges"], "nudges", today),
             f"Needs a nudge · {fmt_ddmmyyyy(today)}"]

    def entry_line(n, e):
        roles = " / ".join(e["roles"])
        bits = [f"{n}. **{html_anchor(e['link'], e['company'])}**",
                esc(roles)]
        label = STAGE_LABEL.get(e["stage"])
        if label:
            bits.append(esc(label))
        bits.append(f"{e['days']}d")
        return " · ".join(bits)

    def section(name, group, meta):
        return {"header": _heading(name, len(group), meta),
                "entries": group, "fmt": entry_line,
                "max": NUDGE_CAP, "floor": 1}

    urgent = [e for e in entries if e["urgent"]]
    applied = [e for e in entries if e["stage"] == "Applied" and not e["urgent"]]
    chase = [e for e in applied if e["days"] >= NUDGE_THRESHOLD]
    week = [e for e in applied if e["days"] < NUDGE_THRESHOLD]
    groups = []
    if urgent:
        metas = []
        if any(e["stage"] == "Callback" for e in urgent):
            metas.append("recruiter waiting")
        if any(e["stage"] == "OA" for e in urgent):
            metas.append("assessment deadline")
        if any(e["stage"] == "Accepted" for e in urgent):
            metas.append("offer decision")
        if any(e["stage"] == "Interview" for e in urgent):
            metas.append("interview prep")
        groups.append(section("Act now", urgent, " · ".join(metas)))
    if chase:
        groups.append(section("Chase now", chase, f"{NUDGE_THRESHOLD}d or more"))
    if week:
        groups.append(section("Chase this week", week,
                              f"under {NUDGE_THRESHOLD}d"))
    for name, stage in (("Reply to recruiter", "Callback"),
                        ("Assessment", "OA"),
                        ("Interview outcome", "Interview"),
                        ("Offer decision", "Accepted"),
                        ("Submit or drop", "Tailored"),
                        ("Tailor or drop", "Found")):
        rest = [e for e in entries if e["stage"] == stage and not e["urgent"]]
        if rest:
            groups.append(section(name, rest, STAGE_LABEL.get(stage, "")))
    lines += _pack(groups, NUDGE_MAX_LINES - 3)  # preface + title + trailer
    lines.append(NUDGE_TRAILER)
    return _emit(lines)


# ----------------------------------------------------- newpost (msg 3)
NEWPOST_TRAILER = ("Reply /tailor 1 2 to tailor the top 2, /tailor all for "
                   "every entry, /skipped to list everything filtered.")
NEWPOST_CAP = 5
NEWPOST_SKIPPED_CAP = 2
NEWPOST_MAX_AGE = 30
RANKINGS_FRESH_HOURS = 48

# Explicit end-date fields that may carry a term close (conservative: only
# these fields trigger the term-ended exclusion; unknown -> keep).
TERM_END_FIELDS = ("term_end", "end", "term_ends", "closes",
                   "closing", "deadline", "close_date", "expiry")
TERM_TEXT_FIELDS = ("term", "dates", "duration", "period")
# Explicit start-date fields (conservative: only these trigger started).
TERM_START_FIELDS = ("term_start", "start", "starts", "start_date",
                     "commencement")

_EXPLICIT_DATE_RE = re.compile(
    r"(\d{1,2}[/-，]\d{1,2}[/-，]\d{2,4})|(\d{4}-\d{2}-\d{2})")


def parse_loose_date(value):
    """Parse DD/MM/YYYY (or ISO) to a date; None when unknown."""
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", s)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, dd)
        except ValueError:
            return None
    return parse_date(s)


def _posting_text_blob(p):
    return " ".join(norm_text(p.get(f, "")) for f in
                      ("role", "location", "link") + TERM_TEXT_FIELDS)


def posting_term_bounds(p):
    """(start, end) from explicit posting fields only; (None, None) unknown."""
    start = end = None
    for f in TERM_START_FIELDS:
        d = parse_loose_date(p.get(f))
        if d and (start is None or d < start):
            start = d
    for f in TERM_END_FIELDS:
        d = parse_loose_date(p.get(f))
        if d and (end is None or d > end):
            end = d
    blob = _posting_text_blob(p)
    for m in _EXPLICIT_DATE_RE.finditer(blob):
        d = parse_loose_date(m.group(0))
        if d and (end is None or d > end):
            # Only treat blob dates as term bounds when a term-ish field
            # exists on the posting (conservative).
            if any(norm_text(p.get(f, "")) for f in TERM_TEXT_FIELDS):
                end = d
    return start, end


def load_rankings(rankings_path, now=None):
    """Rankings join: {normlink: score,...} or None when missing/stale.

    Fresh = file mtime within ``RANKINGS_FRESH_HOURS`` of now."""
    try:
        st = os.stat(rankings_path)
    except OSError:
        return None, False
    ref = now or datetime.now(_SGT)
    try:
        mtime = datetime.fromtimestamp(st.st_mtime, tz=_SGT)
    except (OSError, ValueError, OverflowError):
        return None, False
    if (ref - mtime).total_seconds() > RANKINGS_FRESH_HOURS * 3600:
        return None, False
    try:
        with open(rankings_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, False
    rows = data.get("ranked", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None, False
    scores = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            score = int(r.get("score", 0))
        except (TypeError, ValueError):
            continue
        link = normalize_url(r.get("link", "")).lower()
        if link:
            scores[link] = score
        company = norm_text(r.get("company", "")).lower()
        role = norm_text(r.get("role", "")).lower()
        if company or role:
            scores.setdefault(f"{company}|{role}", score)
    return scores, True


def load_skipped(skipped_path):
    """Lenient skipped.yaml read: list of mappings; anything else -> []."""
    try:
        with open(skipped_path, encoding="utf-8") as fh:
            items = parse_yaml_list(fh.read())
    except OSError:
        return []
    return [i for i in items if isinstance(i, dict)]


def skipped_keys(skipped):
    links, names, meta = set(), {}, set()
    for s in skipped:
        link = normalize_url(s.get("link", "")).lower()
        if link:
            links.add(link)
        company = norm_text(s.get("company", "")).lower()
        role = norm_text(s.get("role", "")).lower()
        if company or role:
            names[f"{company}|{role}"] = s
        meta.add(link or f"{company}|{role}")
    return links, names


def skipped_short(entry):
    """Reason-short vocabulary for skipped lines."""
    if not isinstance(entry, dict):
        return "skipped"
    d = fmt_ddmmyyyy(entry.get("skipped_at", "")) if entry.get("skipped_at") else ""
    kind = norm_text(entry.get("skip_kind", ""))
    if d or kind:
        return f"skipped {d} ({kind})" if d and kind else f"skipped {d or kind}"
    return "skipped"


def compute_newpost(postings, apps, skipped, today, scores=None, prs=None):
    """Eligibility join; returns (eligible, skipped_list, ranked_bool)."""
    links, names = tracker_keys([a for a in apps if isinstance(a, dict)])
    sk_links, sk_names = skipped_keys(skipped)
    eff = effective_tailor_prs(prs, apps) if prs is not None else []
    pr_links, pr_slugs = set(), {}
    for p in eff:
        for bl in pr_body_links(p):
            pr_links.add(bl)
            pr_slugs[bl] = p.get("number")
        pr_slugs[pr_branch_slug(p)] = p.get("number")
    eligible, skipped_list = [], []

    def skip(p, reason):
        skipped_list.append({"company": norm_text(p.get("company", "?")),
                             "role": norm_text(p.get("role", "?")),
                             "reason": reason})
    for p in postings:
        if not isinstance(p, dict):
            continue
        link = normalize_url(p.get("link", "")).lower()
        company = norm_text(p.get("company", "")).lower()
        role = norm_text(p.get("role", "")).lower()
        namekey = f"{company}|{role}"
        if (link and link in links) or namekey in names:
            skip(p, "already tracked")
            continue
        if (link and link in sk_links) or namekey in sk_names:
            entry = sk_names.get(namekey)
            if entry is None:
                entry = {"skipped_at": "", "skip_kind": ""}
                for s in skipped:
                    if normalize_url(s.get("link", "")).lower() == link:
                        entry = s
                        break
            skip(p, skipped_short(entry))
            continue
        pr_no = pr_slugs.get(link) or pr_slugs.get(work_slug(p))
        if link and link in pr_links:
            for bl, no in pr_slugs.items():
                if bl == link:
                    pr_no = no
                    break
        if pr_no is not None or posting_pr_matched(
                p, pr_links, set(pr_slugs)):
            skip(p, f"open tailor PR #{pr_no}" if pr_no else "open tailor PR")
            continue
        d_added = parse_date(p.get("added")) or parse_date(p.get("posted"))
        if d_added and (today - d_added).days > NEWPOST_MAX_AGE:
            skip(p, "older than 30 days")
            continue
        start, end = posting_term_bounds(p)
        if end and end < today:
            skip(p, f"term ended {fmt_ddmmyyyy(end)}")
            continue
        if start and start <= today and end is None:
            # Started with no known end: conservative keep (unknown).
            pass
        score = None
        if scores is not None:
            score = scores.get(link, scores.get(namekey))
        eligible.append({"posting": p,
                         "score": score,
                         "added": d_added})
    ranked = scores is not None
    if ranked:
        eligible.sort(key=lambda e: (-(e["score"] if e["score"] is not None
                                            else -1),
                                     -(e["added"].toordinal()
                                       if e["added"] else 0)))
    else:
        eligible.sort(key=lambda e: (-(e["added"].toordinal())
                                     if e["added"] else 0))
    return eligible, skipped_list, ranked


def _posted_age(added, today):
    if not added:
        return ""
    n = (today - added).days
    if n <= 0:
        return "posted today"
    if n == 1:
        return "posted 1d ago"
    return f"posted {n}d ago"


def _posted_age_short(added, today):
    """Compact posted age for the entry tuple: ``today`` / ``22d``."""
    if not added:
        return ""
    n = (today - added).days
    return "today" if n <= 0 else f"{n}d"


def _fit_tally(eligible, ranked):
    """One-line tally: total new, then strong / good / rest buckets."""
    total = len(eligible)
    if not ranked:
        return f"{total} new · sorted by posted date"
    scores = [e["score"] for e in eligible if e["score"] is not None]
    strong = sum(1 for v in scores if v >= 95)
    good = sum(1 for v in scores if 85 <= v < 95)
    rest = total - strong - good
    return (f"{total} new · {strong} strong · {good} good · {rest} rest · "
            f"sorted by fit")


def render_newpost(eligible, skipped_list, ranked, today):
    """Message 3 - new postings, Option A layout. Silent when empty.

    A one-line tally leads (`133 new · 6 strong · 13 good · 114 rest`), then
    a heading carrying the fit floor and the ``+N not shown`` notice, then
    the top ``NEWPOST_CAP`` by fit score, then at most
    ``NEWPOST_SKIPPED_CAP`` Skipped lines with a tail.
    """
    if not eligible:
        return ""
    lines = [_preface(PREFACE_AGENT["newpost"], "newpost", today),
             f"New postings · {fmt_ddmmyyyy(today)}",
             _fit_tally(eligible, ranked)]
    shown = eligible[:NEWPOST_CAP]
    floor = min((e["score"] for e in shown if e["score"] is not None),
                default=None)
    heading = _heading("Worth applying",
                      None if floor is None else f"fit {floor}+")
    lines.append(heading(len(shown), len(eligible) - len(shown)))
    for i, e in enumerate(shown, 1):
        p = e["posting"]
        bits = [f"{i}. "
                f"**{html_anchor(p.get('link', ''), p.get('company', '?'))}** · "
                f"{esc(p.get('role', '?') or '?')}"]
        extras = []
        if ranked and e["score"] is not None:
            extras.append(f"{e['score']}")
        term = norm_text(next((p.get(f, "") for f in TERM_TEXT_FIELDS
                              if norm_text(p.get(f, ""))), ""))
        if term:
            extras.append(esc(term))
        age = _posted_age_short(e["added"], today)
        if age:
            extras.append(age)
        for f in TERM_END_FIELDS:
            if parse_loose_date(p.get(f)):
                extras.append(f"closes {fmt_ddmmyyyy(p.get(f))}")
                break
        if extras:
            bits.append(" · ".join(extras))
        lines.append(" · ".join(bits))
    for s in skipped_list[:NEWPOST_SKIPPED_CAP]:
        lines.append(f"Skipped: {esc(s['company'])} · "
                     f"{esc(s['role'])} ({esc(s['reason'])})")
    if len(skipped_list) > NEWPOST_SKIPPED_CAP:
        lines.append(f"+{len(skipped_list) - NEWPOST_SKIPPED_CAP} more skipped")
    lines.append(NEWPOST_TRAILER)
    return _emit(lines)


# ---------------------------------------------------------------- cli
SKIPPED_DEFAULT = os.environ.get("JOBSEARCH_SKIPPED",
                                  os.path.join(HERE, "skipped.yaml"))
RANKINGS_DEFAULT = os.environ.get("JOBSEARCH_RANKINGS",
                                    os.path.join(HERE, "rankings.json"))
MODES = ("digest", "nudges", "newpost")


def build_parser():
    ap = argparse.ArgumentParser(
        description="Job-search Telegram notifications: digest / nudges / "
                      "newpost (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="digest",
                    help="subcommand: digest (default), nudges, newpost")
    ap.add_argument("--mode", dest="mode_flag", default=None,
                    choices=list(MODES),
                    help="same as the positional subcommand; flag wins")
    ap.add_argument("--repo", default=REPO_DIR,
                    help="resume clone path (default: ./repo, env JOBSEARCH_REPO)")
    ap.add_argument("--days", type=int, default=7,
                    help="age in days after which an Applied entry awaits a "
                         "reply (default: 7)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the digest but do not advance the state file")
    ap.add_argument("--json", action="store_true",
                    help="emit the digest data as JSON instead of text")
    ap.add_argument("--skipped", default=SKIPPED_DEFAULT,
                    help="owner-skip list (default: ./skipped.yaml)")
    ap.add_argument("--rankings", default=RANKINGS_DEFAULT,
                    help="rankings join file (default: ./rankings.json)")
    return ap


def resolve_mode(args):
    """Flag wins over positional; bare invocation = digest."""
    if getattr(args, "mode_flag", None):
        return args.mode_flag
    mode = (args.mode or "digest").lower()
    return mode if mode in MODES else "digest"


def run_digest(args, log):
    apps, postings = load_inputs(args.repo, log=log)
    today = sgt_today()
    stored = read_state(STATE_FILE)
    # No baseline yet: treat yesterday as the previous digest so only today's
    # submissions are announced (never replay history).
    last = stored if stored is not None else today - timedelta(days=1)
    if last > today:
        last = today - timedelta(days=1)

    prs = fetch_open_tailor_prs(log=log)
    if prs is None:
        log("summary: tailor-PR check unavailable; tracker-only digest")
    res = compute(apps, postings, last, today, args.days, prs=prs)
    out = render_json(res) if args.json else render_text(res)
    if out:
        print(out)

    if not args.dry_run:
        write_state(STATE_FILE, today,
                    datetime.now(_SGT).isoformat(timespec="seconds"))
    return 0


def run_nudges(args, log):
    apps, _postings = load_inputs(args.repo, log=log)
    today = sgt_today()
    entries, _silent = compute_nudges(apps, today)
    if args.json:
        print(json.dumps({
            "date": today.isoformat(),
            "entries": [{"company": e["company"],
                           "roles": e["roles"], "link": e["link"],
                           "applied_date": e["date"].isoformat(),
                           "days_waiting": e["days"],
                           "action": e["action"]} for e in entries],
            "silent": not entries,
        }, indent=2, ensure_ascii=False))
        return 0
    out = render_nudges(entries, today)
    if out:
        print(out)
    return 0  # read-only: never touches the state file


def run_newpost(args, log):
    apps, postings = load_inputs(args.repo, log=log)
    today = sgt_today()
    skipped = load_skipped(args.skipped)
    scores, fresh = load_rankings(args.rankings)
    if scores is None:
        log("summary: rankings unavailable/stale; sorting by posted date")
    prs = fetch_open_tailor_prs(log=log)
    if prs is None:
        log("summary: tailor-PR check unavailable; tracker-only exclusion")
    eligible, skipped_list, ranked = compute_newpost(
        postings, apps, skipped, today, scores=scores, prs=prs)
    if args.json:
        print(json.dumps({
            "date": today.isoformat(),
            "ranked": ranked,
            "eligible": [{"company": e["posting"].get("company"),
                            "role": e["posting"].get("role"),
                            "link": e["posting"].get("link"),
                            "score": e["score"]} for e in eligible],
            "skipped": skipped_list,
            "silent": not eligible,
        }, indent=2, ensure_ascii=False))
        return 0
    out = render_newpost(eligible, skipped_list, ranked, today)
    if out:
        print(out)
    return 0  # read-only: never touches the state file


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)
    if args.days < 0:
        log("summary: --days must be >= 0")
        return 2
    mode = resolve_mode(args)
    if mode == "nudges":
        return run_nudges(args, log)
    if mode == "newpost":
        return run_newpost(args, log)
    return run_digest(args, log)


if __name__ == "__main__":
    sys.exit(main())
