#!/usr/bin/env python3
"""Local job-search dashboard: kanban board + source->status sankey (stdlib only).

Renders a **single self-contained** HTML file (inline CSS + inline SVG, a few
lines of vanilla JS, no CDN, no external assets -- the Pi renders it offline).

Data sources (all read-only):
  * ``repo/data/postings.json`` -- discovered postings (company/role/source/...)
  * ``repo/applications.yaml``  -- the application tracker
  * ``queue.py list --json``    -- drain-queue state (subprocess, best effort)

Status/PR logic is **not duplicated**: it is imported from ``summary.py``
(``parse_yaml_list``, ``canonical_status``, ``tracker_keys``, ``is_tracked``,
``effective_tailor_prs``, ``fetch_open_tailor_prs``, ...). When the gh check or
the queue subprocess is unavailable the board degrades gracefully: the board is
still rendered, without queue and/or PR data, and the reason is logged to
stderr.

PII policy: this module never reads ``profile.local.yaml`` and renders only
company / role / status / date / PR link. No NRIC, address, phone, email.

Usage:
  dashboard.py [--repo DIR] [--out PATH] [--json] [--queue-cmd CMD]
"""
import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys

import summary as s

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.environ.get("JOBSEARCH_REPO", os.path.join(HERE, "repo"))
OUT_PATH = os.environ.get("JOBSEARCH_DASHBOARD_OUT",
                          os.path.join(HERE, "dashboard", "index.html"))
QUEUE_CMD = ["python3", os.path.join(HERE, "queue.py"), "list", "--json"]

# Kanban columns, in fixed display order (summary.ORDER).
COLUMNS = list(s.ORDER)

# Queue statuses that mean the item was already submitted; anything else
# (queued / in_progress / failed) stays on Tailored until the tracker advances.
QUEUE_SUBMITTED = "submitted"

SOURCE_MIN = 3   # sources with fewer postings collapse into "Other"
OTHER = "Other"

# Named SVG colours (avoid the '#' in the inline HTML doc; fill via attribute).
FLOW_COLORS = ["#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2",
               "#b279a2", "#eeca3b", "#9d755d", "#bab0ac"]


# ---------------------------------------------------------------- queue input
def load_queue(cmd=None, log=lambda *a: None):
    """Drain-queue items from ``queue.py list --json``, or ``[]`` on failure.

    Any failure (missing script, non-zero exit, timeout, bad JSON) degrades to
    an empty list so the board still renders; the reason goes to ``log``.
    """
    argv = list(cmd if cmd is not None else QUEUE_CMD)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 - missing python/queue.py is expected
        log(f"dashboard: queue check failed: {exc}")
        return []
    if proc.returncode != 0:
        log(f"dashboard: queue check failed: {proc.stderr.strip() or proc.returncode}")
        return []
    try:
        items = json.loads(proc.stdout or "[]")
    except ValueError:
        log("dashboard: queue check returned invalid JSON")
        return []
    return [it for it in items if isinstance(it, dict)]


# ---------------------------------------------------------------- date / age
def card_age(card, today):
    """Days in the card's current status, with the date it is measured from."""
    for key in ("applied_date", "enqueued_at", "added"):
        d = s.parse_date(card.get(key))
        if d is not None:
            return max(0, (today - d).days), key
    return None, None


def card_pr_number(card):
    """PR number as an int, or None (accepts a URL in ``pr``/``pr_number``)."""
    for key in ("pr_number", "pr"):
        v = card.get(key)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        if v.isdigit():
            return int(v)
        tail = v.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def card_pr_url(card):
    """Canonical PR URL: the tracker/queue ``pr`` field, else number-derived."""
    v = str(card.get("pr") or "").strip()
    if v.startswith("http"):
        return v
    num = card_pr_number(card)
    if num is not None:
        return f"https://github.com/{os.environ.get('JOBSEARCH_GH_REPO', s.GH_REPO_DEFAULT)}/pull/{num}"
    return None


# ---------------------------------------------------------------- board build
def _card(status, company, role, date_key=None, date_val=None, pr=None, today=None):
    card = {
        "company": s.norm_text(company) or "?",
        "role": s.norm_text(role) or "?",
        "status": status,
        "applied_date": date_val if date_key == "applied_date" else None,
        "enqueued_at": date_val if date_key == "enqueued_at" else None,
        "added": date_val if date_key == "added" else None,
        "pr": pr,
    }
    days, src = card_age(card, today) if today is not None else (None, None)
    card["days"] = days
    card["age_from"] = src
    return card


def build_board(apps, postings, prs, queue, today, links=None):
    """Compose the six kanban columns as lists of cards.

    * tailors   -- ``effective_tailor_prs`` (open PRs not yet in the tracker)
    * queue     -- queue items keyed by work slug / PR number (submission data)
    * Tailored  -- tracker entries bucketed Tailored + each effective open PR
    * Found     -- postings with no tracker entry and no open tailor PR
    * other     -- tracker rows in Applied/Interview/Offer/Rejected
    """
    apps = [a for a in apps if isinstance(a, dict)]
    postings = [p for p in postings if isinstance(p, dict)]

    # ``tracked_links`` is the tracker's own link set; ``links`` stays the
    # caller's out-dict for posting-link -> column (do not shadow it, or the
    # sankey loses every Found/Tailored posting).
    tracked_links, names = s.tracker_keys(apps)
    tracker_slugs = {s.work_slug(a) for a in apps}
    tracker_pr_nums = s.tracker_pr_numbers(apps)
    tailor_prs = s.effective_tailor_prs(prs, apps) if prs is not None else []
    pr_nums = {p.get("number") for p in tailor_prs}
    pr_links, pr_slugs = set(), set()
    for p in tailor_prs:
        pr_links |= s.pr_body_links(p)
        pr_slugs.add(s.pr_branch_slug(p))

    def best_queue(app):
        """Queue item for a tracker/PR app: work-slug first, then PR number."""
        if not queue:
            return None
        slug = s.work_slug(app)
        for it in queue:
            if slug and (s.work_slug(it) == slug or s.pr_branch_slug(it) == slug):
                return it
        num = card_pr_number(app)
        if num is not None:
            for it in queue:
                if it.get("pr_number") == num:
                    return it
        return None

    board = {k: [] for k in COLUMNS}
    card_links = {}   # normalised posting link -> column, for the sankey join

    def pr_for_posting(posting):
        """The open tailor PR covering this posting, as a URL (or None)."""
        link = s.normalize_url(posting.get("link", "")).lower()
        slug = s.work_slug(posting)
        for pr in tailor_prs:
            if (s.posting_pr_matched(posting, s.pr_body_links(pr),
                                     {s.pr_branch_slug(pr)})
                    or link and link in s.pr_body_links(pr)
                    or s.pr_branch_slug(pr) == slug):
                num = pr.get("number")
                repo = os.environ.get("JOBSEARCH_GH_REPO", s.GH_REPO_DEFAULT)
                return f"https://github.com/{repo}/pull/{num}"
        return None

    def place(posting, status, date_val, pr=None):
        """Add a posting-derived card and remember its link for the sankey."""
        board[status].append(_card(status, posting.get("company"),
                                   posting.get("role"), "added", date_val,
                                   pr, today))
        key = s.normalize_url(posting.get("link", "")).lower()
        if key:
            card_links[key] = status

    # Unfinished queue items (merged tailor PR, submission pending/failed) are
    # tailored work in flight: their postings must leave Found and show as
    # Tailored cards with the queue state. Keyed by PR number and work slug.
    q_unfinished = [q for q in (queue or [])
                    if isinstance(q, dict)
                    and str(q.get("status", "")) in ("queued", "in_progress", "failed")]
    q_pr_nums = {q.get("pr_number") for q in q_unfinished}
    q_slugs = {s.pr_branch_slug(q) for q in q_unfinished
               if s.pr_branch_slug(q).startswith("app/")}
    q_links = {s.normalize_url(str(q.get("link") or "")).lower()
               for q in q_unfinished if q.get("link")}

    def queue_in_flight(q):
        """True when this queue item is represented by an open PR or a
        tracker entry -- a queue-derived card would then be a duplicate."""
        if prs is not None and q.get("pr_number") in pr_nums:
            return True
        if q.get("pr_number") in tracker_pr_nums:
            return True
        if s.work_slug(q) in tracker_slugs:
            return True
        slug = s.pr_branch_slug(q)
        if slug and slug in tracker_slugs:
            return True
        link = s.normalize_url(str(q.get("link") or "")).lower()
        return bool(link and link in tracked_links)

    # Queue items still in flight get a Tailored card only when they are not
    # already shown via an open tailor PR or a tracker entry.
    for q in q_unfinished:
        if queue_in_flight(q):
            continue
        note = {"failed": "blocked on submission",
                "in_progress": "submitting"}.get(str(q.get("status")),
                                                 "awaiting submission")
        board["Tailored"].append(_card(
            "Tailored", q.get("company"),
            f"{s.norm_text(q.get('role', '')) or '?'} ({note})",
            "added", str(q.get("enqueued_at") or "")[:10],
            str(q.get("pr") or "") or None, today))

    # Found: postings neither tracked, nor PR-covered, nor queue-unfinished.
    for p in postings:
        if s.is_tracked(p, tracked_links, names):
            continue
        if prs is not None and s.posting_pr_matched(p, pr_links, pr_slugs):
            continue
        plink = s.normalize_url(str(p.get("link") or "")).lower()
        if plink and plink in q_links:
            continue
        if s.work_slug(p) in q_slugs:
            continue
        place(p, "Found", p.get("added"))

    # Postings an open tailor PR covers (matched by body link or work slug).
    for p in postings:
        if s.is_tracked(p, tracked_links, names):
            continue
        if prs is not None and s.posting_pr_matched(p, pr_links, pr_slugs):
            place(p, "Tailored", p.get("added"), pr_for_posting(p))

    # Postings already in the tracker: record the link join for the sankey
    # only — the tracker loop below adds each tracked row exactly once, so
    # adding a posting-level card here would double-count it.
    for p in postings:
        key = s.normalize_url(p.get("link", "")).lower()
        if not key or key not in tracked_links:
            continue
        for a in apps:
            if s.normalize_url(a.get("link", "")).lower() == key:
                bucket = s.row_stage(a)
                if bucket:
                    card_links[key] = bucket
                break

    # Tailored + submitted statuses from the tracker.
    for a in apps:
        bucket = s.row_stage(a)
        if not bucket:
            continue
        q = best_queue(a)
        card = _card(bucket, a.get("company"), a.get("role"),
                     "applied_date", a.get("applied_date"),
                     card_pr_url(a), today)
        if bucket == "Tailored":
            if q and q.get("enqueued_at") and not card["applied_date"]:
                card["enqueued_at"] = q["enqueued_at"]
                card["days"], card["age_from"] = card_age(card, today)
            if not card["pr"] and q:
                card["pr"] = card_pr_url(q)
            if q and q.get("status") == QUEUE_SUBMITTED and not card["applied_date"]:
                card["enqueued_at"] = q.get("submitted_at") or card["enqueued_at"]
                card["days"], card["age_from"] = card_age(card, today)
            # Tracker-Tailored entries always belong on the board, with or
            # without a PR link / queue item (summary.py counts them too).
            board["Tailored"].append(card)
            cl = s.normalize_url(a.get("link", "")).lower()
            if cl:
                card_links[cl] = "Tailored"
        else:
            board[bucket].append(card)
            cl = s.normalize_url(a.get("link", "")).lower()
            if cl:
                card_links[cl] = bucket

    # Tailored: one card per effective open tailor PR that matched no untracked
    # posting — those PRs' postings no longer exist in postings.json (purged or
    # re-keyed), so the PR card is the only trace of that tailored CV. PRs
    # whose posting IS on the board are already represented by it.
    for pr in tailor_prs:
        one_links = s.pr_body_links(pr)
        one_slug = {s.pr_branch_slug(pr)}
        if any(s.posting_pr_matched(p, one_links, one_slug) for p in postings
               if isinstance(p, dict) and not s.is_tracked(p, tracked_links, names)):
            continue
        num = pr.get("number")
        url = f"https://github.com/{os.environ.get('JOBSEARCH_GH_REPO', s.GH_REPO_DEFAULT)}/pull/{num}"
        work = str(pr.get("title") or "")
        work = work.split(": tailored", 1)[0]
        work = work[work.find("(") + 1:work.rfind(")")] if "(" in work else ""
        board["Tailored"].append(_card("Tailored", work or "tailored CV",
                                       "tailored CV (posting gone)",
                                       None, None, url, today))

    for col in board:
        board[col].sort(key=lambda c: (c.get("days") is None, -(c.get("days") or 0),
                                       c["company"].lower(), c["role"].lower()))
    if links is not None:
        links.update(card_links)
    return board


# ---------------------------------------------------------------- sankey
def bucket_sources(postings, min_count=SOURCE_MIN):
    """Left-side sankey buckets: a source, or "Other" when it has < min_count."""
    counts = {}
    for p in postings:
        if not isinstance(p, dict):
            continue
        src = s.norm_text(p.get("source")) or "Unknown"
        counts[src] = counts.get(src, 0) + 1
    out = {}
    for p in postings:
        if not isinstance(p, dict):
            continue
        src = s.norm_text(p.get("source")) or "Unknown"
        out.setdefault(OTHER if counts[src] < min_count else src, 0)
        out[OTHER if counts[src] < min_count else src] += 1
    return out


def build_sankey(postings, board, min_count=SOURCE_MIN, status_of=None):
    """Flows from posting source -> current status bucket, with counts.

    Each posting contributes 1. ``status_of`` maps a normalised posting link to
    its kanban column (built by :func:`build_board`); linked cards map directly.
    Only the real source (not a collapsed bucket) is deterministic here.
    """
    status_of = dict(status_of or {})
    for col, cards in board.items():
        for c in cards:
            if c.get("link"):
                status_of.setdefault(s.normalize_url(c["link"]).lower(), col)
    sources = {}
    flows = {}
    for p in postings:
        if not isinstance(p, dict):
            continue
        src = s.norm_text(p.get("source")) or "Unknown"
        key = s.normalize_url(p.get("link", "")).lower()
        status = status_of.get(key, "Applied")
        sources[src] = sources.get(src, 0) + 1
        flows[(src, status)] = flows.get((src, status), 0) + 1
    # Collapse sources with < min_count postings into a single "Other" band,
    # moving (not copying) their flow weight so totals stay == len(postings).
    major = sorted({k[0] for k in flows if sources.get(k[0], 0) >= min_count})
    collapsed = {}      # status -> n for all minor sources
    kept = {}           # flows for major sources only
    for (src, status), n in flows.items():
        if src in major:
            kept[(src, status)] = n
        else:
            collapsed[status] = collapsed.get(status, 0) + n
    for status, n in collapsed.items():
        kept[(OTHER, status)] = kept.get((OTHER, status), 0) + n
    flows = kept
    left = major + ([OTHER] if collapsed else [])
    left = [n for n in left if any((n, c) in flows for c in COLUMNS)]
    right = [c for c in COLUMNS if any((n, c) in flows for n in left)]
    return {"left": left, "right": right,
            "flows": [{"from": a, "to": b, "count": n}
                      for (a, b), n in sorted(flows.items()) if n]}


# ---------------------------------------------------------------- svg
def _layout(counts, gap, pad, min_band):
    n = max(1, len(counts))
    total = sum(counts.values()) or 1
    avail = min_band * n + gap * (n - 1)
    space = max(avail, 160)
    scale = (space - gap * (n - 1)) / total
    ys, y = {}, pad
    for name in counts:
        h = max(4, counts[name] * scale)
        ys[name] = (y, h)
        y += h + gap
    return ys, y


def render_sankey_svg(sankey, width=860, pad=8):
    """Hand-rolled inline-SVG sankey. Returns (svg_string, height)."""
    flows = sankey["flows"]
    if not flows:
        return ("<svg class='sankey' viewBox='0 0 860 60' role='img' "
                "aria-label='no postings'>"
                "<text x='12' y='34'>No postings to chart.</text></svg>", 60)
    lc, rc = {}, {}
    for f in flows:
        lc[f["from"]] = lc.get(f["from"], 0) + f["count"]
        rc[f["to"]] = rc.get(f["to"], 0) + f["count"]
    ly, lh = _layout(lc, gap=10, pad=pad, min_band=14)
    ry, rh = _layout(rc, gap=10, pad=pad, min_band=14)
    height = int(max(lh, rh) + pad * 2)
    lx, rx, nw = 8, width - 8, 12
    colors = {n: FLOW_COLORS[i % len(FLOW_COLORS)] for i, n in enumerate(lc)}
    off_l = {n: ly[n][0] for n in lc}
    off_r = {n: ry[n][0] for n in rc}
    total = sum(lc.values()) or 1
    scale_l = (max(1, lh - 10 * (len(lc) - 1)) / total) if len(lc) > 1 else 0
    scale_l = scale_l or (lc and (lh / total)) or 1
    total_r = sum(rc.values()) or 1
    scale_r = (rh / total_r) if len(rc) == 1 else \
        ((max(1, rh - 10 * (len(rc) - 1))) / total_r)
    parts = [f"<svg class='sankey' viewBox='0 0 {width} {height}' role='img' "
             f"aria-label='posting source to status flows'>"]
    for f in sorted(flows, key=lambda f: (ly[f["from"]][0], ry[f["to"]][0])):
        h = max(1.5, f["count"] * ((lh / total) if len(lc) == 1 else scale_l))
        y1 = off_l[f["from"]]
        y2 = off_r[f["to"]]
        off_l[f["from"]] += h
        off_r[f["to"]] += h
        x1, x2 = lx + nw, rx - nw
        cx = (x1 + x2) / 2
        col = colors.get(f["from"], FLOW_COLORS[0])
        parts.append(
            f"<path d='M{x1:.1f},{y1:.1f} C{cx:.1f},{y1:.1f} {cx:.1f},{y2:.1f} "
            f"{x2:.1f},{y2:.1f} L{x2:.1f},{y2 + h:.1f} C{cx:.1f},{y2 + h:.1f} "
            f"{cx:.1f},{y1 + h:.1f} {x1:.1f},{y1 + h:.1f} Z' "
            f"fill='{col}' fill-opacity='0.55' stroke='{col}' stroke-width='.4'>"
            f"<title>{html.escape(f['from'])} \u2192 {html.escape(f['to'])}: "
            f"{f['count']}</title></path>")
        parts.append(f"<text x='{(x1 + x2) / 2:.1f}' y='{(y1 + y2) / 2 - 1:.1f}' "
                     f"font-size='10' fill='currentColor' text-anchor='middle'>"
                     f"{f['count']}</text>")
    for name, (y, h) in ly.items():
        parts.append(f"<rect x='{lx}' y='{y:.1f}' width='{nw}' height='{h:.1f}' "
                     f"fill='{colors[name]}'/>")
        parts.append(f"<text x='{lx + nw + 6}' y='{y + h / 2 + 4:.1f}' font-size='12' "
                     f"fill='currentColor'>{html.escape(name)}"
                     f"<tspan font-size='10' opacity='.75'> ({lc[name]})</tspan></text>")
    for name, (y, h) in ry.items():
        col = FLOW_COLORS[[*rc].index(name) % len(FLOW_COLORS)]
        parts.append(f"<rect x='{rx - nw}' y='{y:.1f}' width='{nw}' height='{h:.1f}' "
                     f"fill='{col}'/>")
        parts.append(f"<text x='{rx - nw - 6}' y='{y + h / 2 + 4:.1f}' font-size='12' "
                     f"fill='currentColor' text-anchor='end'>{html.escape(name)}"
                     f"<tspan font-size='10' opacity='.75'> ({rc[name]})</tspan></text>")
    parts.append("</svg>")
    return "".join(parts), height


# ---------------------------------------------------------------- html
CSS = """
:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:#0f1115;color:#e6e6e6}
header{padding:18px 20px 8px}
h1{margin:0 0 4px;font-size:20px}
h2{margin:26px 20px 10px;font-size:16px;color:#cdd3db}
.meta{color:#8b949e;font-size:12px}
.board{display:flex;gap:12px;padding:8px 20px 4px;align-items:flex-start;overflow-x:auto}
.col{background:#161b22;border:1px solid #232a33;border-radius:10px;min-width:210px;flex:1 1 210px}
.col h3{margin:0;padding:10px 12px;font-size:13px;border-bottom:1px solid #232a33;display:flex;justify-content:space-between}
.count{color:#8b949e;font-variant-numeric:tabular-nums}
.cards{list-style:none;margin:0;padding:8px;display:flex;flex-direction:column;gap:8px;max-height:62vh;overflow:auto}
.card{background:#1e242d;border:1px solid #2b333d;border-radius:8px;padding:8px 10px}
.card .co{font-weight:600}
.card .ro{color:#b8c0ca;font-size:12px;margin-top:1px}
.card .foot{display:flex;justify-content:space-between;gap:6px;margin-top:6px;color:#8b949e;font-size:11px}
.card a{color:#6ea8fe;text-decoration:none}
.card a:hover{text-decoration:underline}
.empty{color:#5a626c;font-size:12px;padding:6px 4px}
.sankey{display:block;width:100%;height:auto;padding:0 20px 20px;color:#e6e6e6}
svg.sankey text{font-family:inherit}
footer{color:#6b7280;font-size:11px;padding:10px 20px 30px}
@media (prefers-color-scheme:light){
body{background:#f6f8fa;color:#1f2328}
.col{background:#fff;border-color:#d0d7de}.col h3{border-color:#d0d7de}
.card{background:#f6f8fa;border-color:#d8dee4}.card .ro{color:#57606a}
.card .foot,.meta,.count,.empty{color:#6e7781}
h2{color:#424a53}.sankey{color:#1f2328}
}
"""


def render_html(data, generated=None):
    """Full self-contained HTML document for the computed board+sankey."""
    board, sankey = data["board"], data["sankey"]
    counts = data["counts"]
    cards_total = sum(counts.values())
    gen = generated or dt.datetime.now().isoformat(timespec="seconds")
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<title>Job search dashboard</title>", f"<style>{CSS}</style></head><body>",
           "<header><h1>Job search dashboard</h1>",
           f"<div class='meta'>Generated {html.escape(gen)} \u00b7 {cards_total} "
           f"cards \u00b7 {sum(f['count'] for f in sankey['flows'])} postings sourced</div>",
           "</header>", "<section class='board'>"]
    for col in COLUMNS:
        cards = board.get(col, [])
        out.append(f"<div class='col'><h3><span>{col}</span>"
                   f"<span class='count'>{len(cards)}</span></h3><ul class='cards'>")
        if not cards:
            out.append("<li class='empty'>none</li>")
        for c in cards:
            out.append("<li class='card'>")
            out.append(f"<div class='co'>{html.escape(c['company'])}</div>")
            out.append(f"<div class='ro'>{html.escape(c['role'])}</div>")
            day = f"{c['days']}d in {c['status'].lower()}" if c["days"] is not None else "no date"
            pr = (f"<a href='{html.escape(c['pr'], quote=True)}'>PR</a>"
                  if c.get("pr") else "")
            out.append(f"<div class='foot'><span>{html.escape(day)}</span>{pr}</div>")
            out.append("</li>")
        out.append("</ul></div>")
    out.append("</section>")
    out.append("<h2>Sankey \u2014 posting source \u2192 status</h2>")
    svg, _h = render_sankey_svg(sankey)
    out.append(svg)
    out.append("<footer>Local dashboard \u2014 company / role / status / date / PR "
               "only. No personal data (no NRIC, address, phone, email).</footer>")
    out.append("<script>document.documentElement.dataset.board=JSON.stringify("
               f"{json.dumps(data)}).length?1:1;</script></body></html>")
    return "".join(out)


# ---------------------------------------------------------------- compute
def build_data(repo_dir=REPO_DIR, queue_cmd=None, log=lambda *a: None,
               today=None, prs="live"):
    """Compute the board + sankey data object. ``prs`` may be injected for tests."""
    apps, postings = s.load_inputs(repo_dir, log=log)
    if prs == "live":
        prs = s.fetch_open_tailor_prs(log=log)
        if prs is None:
            log("dashboard: tailor-PR check unavailable; Found may over-count")
    queue = load_queue(queue_cmd, log=log)
    if not queue:
        log("dashboard: no queue data; Tailored cards lack submission dates")
    today = today or s.sgt_today()
    card_links = {}
    board = build_board(apps, postings, prs, queue, today, links=card_links)
    counts = {k: len(v) for k, v in board.items()}
    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "counts": counts,
        "board": board,
        "sankey": build_sankey(postings, board, status_of=card_links),
        "degraded": {"gh": prs is None, "queue": not queue},
    }


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(
        description="Render the local job-search dashboard (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO_DIR,
                    help="resume clone path (default: ./repo, env JOBSEARCH_REPO)")
    ap.add_argument("--out", default=OUT_PATH,
                    help="output HTML path (default: ./dashboard/index.html)")
    ap.add_argument("--json", action="store_true",
                    help="print the computed board+sankey data object instead")
    ap.add_argument("--queue-cmd", default=None,
                    help="override the queue command (space-separated); for tests")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)  # noqa: E731
    queue_cmd = args.queue_cmd.split() if args.queue_cmd else None
    data = build_data(args.repo, queue_cmd, log=log)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    html_doc = render_html(data, generated=data.get("generated"))
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    os.replace(tmp, args.out)
    log(f"dashboard: wrote {args.out} ({len(html_doc.encode('utf-8'))} bytes) "
        f"counts={data['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
