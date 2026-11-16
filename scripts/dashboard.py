#!/usr/bin/env python3
"""Private, cached-only job-search dashboard plus an explicit legacy collector.

The ordinary CLI and HTTP reader consume the shared, sanitized projection.
Rendering is deterministic for a fixed snapshot and as-of date. The optional
``--legacy-collect`` path retains the old source collector for migration use.

The output is one self-contained HTML document with inline CSS and SVG. No
browser-side source reads, network calls, framework, or state-changing control.

Usage:
  dashboard.py [--db PATH] [--out PATH] [--json]
  dashboard.py --legacy-collect [--repo DIR] [--out PATH] [--json]
"""
import argparse
import copy
import datetime as dt
import html
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from urllib.parse import urlsplit

import summary as s
from config import default_repo_dir, default_runtime_path, path_setting

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = path_setting("JOBSEARCH_REPO", default_repo_dir())
OUT_PATH = path_setting("JOBSEARCH_DASHBOARD_OUT",
                        default_runtime_path(os.path.join("dashboard", "index.html")))
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
    """Return elapsed days and their actual date basis; never imply a stage age."""
    for key in ("stage_since", "applied_date", "enqueued_at", "added"):
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
        return f"https://github.com/{s.GH_REPO_DEFAULT}/pull/{num}"
    return None


# ---------------------------------------------------------------- board build
def _card(status, company, role, date_key=None, date_val=None, pr=None, today=None,
          stage_since=None):
    card = {
        "company": s.norm_text(company) or "?",
        "role": s.norm_text(role) or "?",
        "status": status,
        "applied_date": date_val if date_key == "applied_date" else None,
        "enqueued_at": date_val if date_key == "enqueued_at" else None,
        "added": date_val if date_key == "added" else None,
        "stage_since": stage_since,
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
                repo = s.GH_REPO_DEFAULT
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
                     card_pr_url(a), today, stage_since=a.get("stage_since"))
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
        url = f"https://github.com/{s.GH_REPO_DEFAULT}/pull/{num}"
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
        status = status_of.get(key, "Unknown/unmatched")
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
    left = [n for n in left if any((n, c) in flows for c in COLUMNS + ["Unknown/unmatched"])]
    right = [c for c in COLUMNS + ["Unknown/unmatched"] if any((n, c) in flows for n in left)]
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
            day = _age_label(c)
            href = _safe_http_url(c.get("pr"))
            pr = (f"<a href='{html.escape(href, quote=True)}' rel='noreferrer'>PR</a>"
                  if href else "")
            out.append(f"<div class='foot'><span>{html.escape(day)}</span>{pr}</div>")
            out.append("</li>")
        out.append("</ul></div>")
    out.append("</section>")
    out.append("<h2>Sankey \u2014 posting source \u2192 current status</h2>")
    svg, _h = render_sankey_svg(sankey)
    out.append(svg)
    out.append("<footer>Private local dashboard \u00b7 read-only sanitized projection</footer>")
    out.append("<script type='application/json' id='dashboard-state'>"
               + _safe_json_for_script({"date": data.get("date"),
                                        "counts": data.get("counts", {})})
               + "</script></body></html>")
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


APPLICATION_STATUS_ORDER = (
    "Found", "Tailored", "Applied", "Callback", "OA", "Interview",
    "Accepted", "Rejected", "Unknown / unmatched",
)
SOURCE_COLORS = ("#2563eb", "#c2410c", "#0f766e", "#7c3aed", "#b42318",
                 "#0e7490", "#475569", "#a16207")

DASHBOARD_CSS = r"""
:root {
  color-scheme: light dark;
  --bg: #f8fafc; --surface: #fff; --surface-muted: #e9eff8;
  --text: #1e293b; --muted: #475569; --border: #cbd5e1;
  --primary: #2563eb; --success: #166534; --warning: #9a3412;
  --danger: #b42318; --focus: #1d4ed8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b1220; --surface: #111c2e; --surface-muted: #18263a;
    --text: #e2e8f0; --muted: #cbd5e1; --border: #42536b;
    --primary: #93c5fd; --success: #86efac; --warning: #fdba74;
    --danger: #fca5a5; --focus: #93c5fd;
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: auto; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: var(--primary); text-decoration-thickness: .08em; text-underline-offset: .16em; }
a:hover { text-decoration-thickness: .14em; }
a:focus-visible, summary:focus-visible {
  outline: 3px solid var(--focus); outline-offset: 3px; border-radius: 4px;
}
.skip { position: absolute; left: -9999px; top: 8px; z-index: 1; padding: 10px 14px; background: var(--surface); }
.skip:focus { left: 8px; }
.shell { width: min(100%, 1440px); margin-inline: auto; padding: 20px 16px 48px; }
.masthead { border-bottom: 1px solid var(--border); padding: 8px 0 18px; }
h1 { margin: 0; font-size: clamp(1.7rem, 4vw, 2.35rem); line-height: 1.2; letter-spacing: -.025em; }
h2 { margin: 0 0 12px; font-size: 1.45rem; line-height: 1.3; }
h3 { margin: 0 0 8px; font-size: 1.08rem; line-height: 1.35; }
p { margin: 8px 0; }
.mast-meta { display: grid; gap: 4px; margin-top: 12px; color: var(--muted); overflow-wrap: anywhere; }
.snapshot-id { font: 0.88rem/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; }
.nav { display: flex; flex-wrap: wrap; gap: 8px; padding: 14px 0; border-bottom: 1px solid var(--border); }
.nav a { display: inline-flex; align-items: center; min-height: 44px; padding: 8px 13px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-weight: 600; text-decoration: none; }
.nav a:hover { background: var(--surface-muted); }
.view { padding: 26px 0 10px; scroll-margin-top: 12px; }
.intro { max-width: 72ch; color: var(--muted); margin: 0 0 16px; }
.metrics { display: grid; grid-template-columns: 1fr; gap: 12px; }
.metric, .panel, .source-card, .application-card, .run-card, .cohort {
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface);
}
.metric { padding: 16px; min-height: 110px; }
.metric-label { display: block; font-size: .9rem; color: var(--muted); font-weight: 600; }
.metric-value { display: block; margin-top: 4px; font-size: 1.45rem; line-height: 1.35; font-variant-numeric: tabular-nums; font-weight: 700; overflow-wrap: anywhere; }
.panel { margin-top: 16px; padding: 16px; }
.source-list { display: grid; gap: 10px; }
.source-card { padding: 12px; min-width: 0; }
.source-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px; }
.source-name { font-weight: 700; }
.state { display: inline-flex; align-items: center; gap: 7px; font-size: .92rem; font-weight: 700; }
.state::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: currentColor; }
.fresh { color: var(--success); }
.stale { color: var(--warning); }
.unavailable, .unknown { color: var(--danger); }
.source-meta, .meta { color: var(--muted); font-size: .92rem; overflow-wrap: anywhere; }
.compact { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: .9rem/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; }
.board { display: grid; grid-template-columns: 1fr; gap: 12px; }
.board-column { min-width: 0; padding: 13px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-muted); }
.board-column h3 { display: flex; justify-content: space-between; gap: 8px; }
.count { color: var(--muted); font-variant-numeric: tabular-nums; }
.application-list, .run-list { display: grid; gap: 10px; }
.application-card, .run-card { padding: 14px; min-width: 0; }
.card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; flex-wrap: wrap; }
.card-title { font-weight: 700; font-size: 1.02rem; overflow-wrap: anywhere; }
.role { color: var(--muted); overflow-wrap: anywhere; }
.badge { display: inline-block; padding: 3px 9px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-muted); font-size: .88rem; font-weight: 650; }
.details { margin-top: 8px; color: var(--muted); overflow-wrap: anywhere; }
.link-list { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 4px; }
.link-list a { display: inline-flex; align-items: center; min-height: 44px; padding: 6px 8px; border-radius: 6px; }
.timeline { margin: 12px 0 0; padding-left: 21px; }
.timeline li { margin: 4px 0; }
.empty { padding: 12px; color: var(--muted); border: 1px dashed var(--border); border-radius: 10px; }
.chart { margin-top: 16px; }
.chart svg { display: block; width: 100%; height: auto; color: var(--text); }
.chart text { fill: currentColor; font: 15px system-ui, sans-serif; }
.chart .flow { fill: none; stroke-linecap: round; opacity: .42; }
.chart .node { fill: var(--surface); stroke: var(--border); stroke-width: 1.5; }
.data-table-wrap { width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 9px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { font-size: .92rem; }
td { overflow-wrap: anywhere; }
caption { padding: 0 0 8px; text-align: left; font-weight: 700; }
.cohorts, .quality-grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
.cohort { padding: 16px; min-width: 0; }
.cohort h3 { font-size: 1.15rem; }
.key-value { display: grid; grid-template-columns: minmax(130px, 1fr) auto; gap: 5px 12px; margin: 10px 0; }
.key-value dt { color: var(--muted); overflow-wrap: anywhere; }
.key-value dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
.reason-list { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; list-style: none; }
.reason-list li { padding: 5px 9px; border: 1px solid var(--border); border-radius: 8px; overflow-wrap: anywhere; }
details { margin-top: 10px; }
summary { min-height: 44px; display: flex; align-items: center; cursor: pointer; font-weight: 650; }
.caveat { color: var(--muted); font-size: .94rem; }
footer { margin-top: 32px; padding-top: 14px; border-top: 1px solid var(--border); color: var(--muted); font-size: .92rem; }
@media (min-width: 48rem) {
  .shell { padding: 28px 24px 56px; }
  .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .source-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .board { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .cohorts, .quality-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (min-width: 64rem) {
  .shell { padding-inline: 32px; }
  .board { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (min-width: 88rem) {
  .board { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
}
"""


def _safe_http_url(value):
    """Return an absolute HTTP(S) URL, excluding credentials and controls."""
    if not isinstance(value, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        return value
    except ValueError:
        return None


def _safe_json_for_script(value):
    """JSON text safe inside an HTML script data block, including hostile ``</script>``."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False)
    return (encoded.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _safe_json_response(value):
    """Serialize an API payload without emitting raw HTML-significant bytes."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return (encoded.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _age_label(card):
    days = card.get("days")
    basis = card.get("age_from")
    if days is None or basis is None:
        return "No date recorded"
    if basis == "stage_since":
        status = card.get("status")
        if status and status != "Unknown / unmatched":
            return "{} days in {} · stage since {}".format(
                days, str(status).lower(), card.get("stage_since"))
        return "{} days since recorded stage date ({})".format(
            days, card.get("stage_since"))
    labels = {"applied_date": "applied date", "enqueued_at": "queue date",
              "added": "posting added date"}
    return "{} days since {}".format(days, labels.get(basis, basis.replace("_", " ")))


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _display(value, fallback="Unknown", limit=240):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return fallback
    result = " ".join(str(value).split())
    if not result:
        return fallback
    if len(result) > limit:
        return result[:limit - 1] + "…"
    return result


def _status_for_row(row):
    return s.row_stage(_as_dict(row)) or "Unknown / unmatched"


def _application_rows(snapshot):
    """Build a display-only board; missing joins never become Applied."""
    facts = _as_dict(snapshot.get("facts"))
    aliases = _as_dict(facts.get("application_aliases"))
    conflicts = set(facts.get("conflicted_application_aliases", []))
    events_by_id = defaultdict(list)
    for event in facts.get("events", []):
        if not isinstance(event, dict):
            continue
        event_id = event.get("application_id")
        if not isinstance(event_id, str) or event_id in conflicts:
            continue
        canonical = aliases.get(event_id, event_id)
        stamp = event.get("occurred_at")
        if isinstance(stamp, str) and stamp:
            events_by_id[canonical].append(event)

    raw_apps = [row for row in facts.get("applications", []) if isinstance(row, dict)]
    known_ids = {row.get("application_id") for row in raw_apps
                 if isinstance(row.get("application_id"), str)}
    attempts_by_id = defaultdict(list)
    unmatched_attempts = []
    for attempt in facts.get("attempts", []):
        if not isinstance(attempt, dict):
            continue
        app_id = attempt.get("application_id")
        if isinstance(app_id, str) and app_id in known_ids:
            attempts_by_id[app_id].append(attempt)
        else:
            unmatched_attempts.append(attempt)

    as_of = snapshot.get("as_of")
    try:
        today = dt.date.fromisoformat(as_of)
    except (TypeError, ValueError):
        today = None

    result = []
    for app in raw_apps:
        row = _as_dict(app.get("row"))
        status = _status_for_row(row)
        days, age_from = card_age(row, today) if today else (None, None)
        timeline = []
        claims = app.get("submission_date_claims", [])
        if isinstance(claims, list) and claims:
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                source = _display(claim.get("source"), "unknown source", 80)
                value = claim.get("value")
                if claim.get("invalid"):
                    timeline.append("Submission date invalid in {} record".format(source))
                elif claim.get("missing") or not value:
                    timeline.append("Submission date unknown in {} record".format(source))
                else:
                    timeline.append("Submission date {} · {} record".format(
                        _display(value, "unknown date", 40), source))
        else:
            timeline.append("Submission date not recorded")
        stage_since = row.get("stage_since")
        timeline.append("Current status {}{}".format(
            status, " since " + _display(stage_since, "", 40) if stage_since else
            " · stage date unknown"))
        app_id = app.get("application_id")
        if isinstance(app_id, str):
            for event in events_by_id.get(app_id, []):
                event_type = _display(event.get("event_type"), "unknown event", 80)
                verified = "verified" if event.get("verification") == "verified" else "unverified"
                timeline.append("{} event · {} · {}".format(
                    _display(event.get("occurred_at"), "unknown date", 48), verified, event_type))
        for attempt in attempts_by_id.get(app_id, []):
            attempt_status = _display(attempt.get("status"), "unknown", 48)
            code = attempt.get("blocker_code")
            timeline.append("Queue attempt: {}{}".format(
                attempt_status, " · blocker " + _display(code, "", 80) if code else ""))
        link = _safe_http_url(row.get("link"))
        pr = _safe_http_url(row.get("pr"))
        if not pr and row.get("pr_number") is not None:
            number = str(row.get("pr_number"))
            if number.isdigit():
                pr = "https://github.com/{}/pull/{}".format(s.GH_REPO_DEFAULT, number)
        result.append({
            "company": _display(row.get("company"), "Company unknown"),
            "role": _display(row.get("role"), "Role unknown"),
            "source": _display(row.get("source"), "Unknown source", 120),
            "status": status,
            "stage_since": _display(stage_since, "", 40) if stage_since else None,
            "days": days,
            "age_from": age_from,
            "age_label": _age_label({"days": days, "age_from": age_from,
                                     "stage_since": stage_since, "status": status}),
            "link": link,
            "pr": pr,
            "timeline": timeline,
        })

    for attempt in unmatched_attempts:
        attempt_status = _display(attempt.get("status"), "unknown", 48)
        code = attempt.get("blocker_code")
        result.append({
            "company": _display(attempt.get("company"), "Company unknown"),
            "role": _display(attempt.get("role"), "Role unknown"),
            "source": _display(attempt.get("source"), "Unknown source", 120),
            "status": "Unknown / unmatched",
            "stage_since": None,
            "days": None,
            "age_from": None,
            "age_label": "No matched application or stage date",
            "link": _safe_http_url(attempt.get("link")),
            "pr": _safe_http_url(attempt.get("pr")),
            "timeline": ["Unmatched queue attempt · {}{}".format(
                attempt_status, " · blocker " + _display(code, "", 80) if code else "")],
        })

    return sorted(result, key=lambda item: (
        APPLICATION_STATUS_ORDER.index(item["status"])
        if item["status"] in APPLICATION_STATUS_ORDER else len(APPLICATION_STATUS_ORDER),
        item["company"].casefold(), item["role"].casefold()))


def build_snapshot_sankey(snapshot):
    """Count unique canonical applications by posting source and current status."""
    facts = _as_dict(snapshot.get("facts"))
    applications = [row for row in facts.get("applications", []) if isinstance(row, dict)]
    flow_counts = Counter()
    for app in applications:
        row = _as_dict(app.get("row"))
        source = _display(row.get("source"), "Unknown source", 120)
        flow_counts[(source, _status_for_row(row))] += 1
    left = sorted({source for source, _status in flow_counts})
    right = [status for status in APPLICATION_STATUS_ORDER
             if any(target == status for _source, target in flow_counts)]
    return {"left": left, "right": right,
            "flows": [{"from": source, "to": status, "count": count}
                      for (source, status), count in sorted(flow_counts.items())],
            "total": len(applications)}


def build_snapshot_data(snapshot, timezone_name=None):
    """Return the deterministic dashboard view model from one shared snapshot."""
    import funnel_report
    import pipeline_health
    import projection

    zone = timezone_name or os.environ.get("JOBSEARCH_TIMEZONE", "Asia/Singapore")
    today = pipeline_health.build_report(
        snapshot, timezone_name=zone, weekday_goal=1, weekend_goal=0,
        ready_buffer=1, as_of=snapshot.get("as_of"))
    funnel = funnel_report.build_report(
        snapshot, as_of=snapshot.get("as_of"), timezone_name=zone)
    traces = projection.build_trace_report(snapshot)
    facts = _as_dict(snapshot.get("facts"))
    quality_source = _as_dict(facts.get("review_quality"))
    quality = {key: value for key, value in quality_source.items() if key != "records"}
    provenance_counts = Counter(
        _display(row.get("provenance_status"), "unknown", 80)
        for row in quality_source.get("records", []) if isinstance(row, dict))
    quality["provenance_status_counts"] = dict(sorted(provenance_counts.items()))
    funnel = copy.deepcopy(funnel)
    for cohort in funnel.get("mature_cohorts", []):
        if isinstance(cohort, dict):
            # The report groups by resume hash for analysis; the dashboard only
            # needs aggregate mature outcomes and coverage, not artifact IDs.
            cohort.pop("grouped_observed_rates", None)
    return {
        "schema_version": 1,
        "snapshot_id": snapshot.get("snapshot_id"),
        "as_of": snapshot.get("as_of"),
        "generated_at": snapshot.get("generated_at"),
        "compact": projection.format_compact(snapshot),
        "today": today,
        "applications": _application_rows(snapshot),
        "sankey": build_snapshot_sankey(snapshot),
        "runs": traces,
        "quality": quality,
        "funnel": funnel,
        "sources": _as_dict(snapshot.get("sources")),
    }


def _e(value):
    return html.escape(str(value), quote=True)


def _count(value):
    return "Unavailable" if value is None else _display(value, "Unavailable", 40)


def _age_seconds(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "age unknown"
    seconds = max(0, int(value))
    if seconds < 3600:
        return "{}m ago".format(seconds // 60)
    if seconds < 86400:
        return "{}h ago".format(seconds // 3600)
    return "{}d ago".format(seconds // 86400)


def _state(value):
    state = value if value in ("fresh", "stale", "unavailable") else "unknown"
    return state, state.capitalize()


def _render_sources(sources):
    out = ["<div class='source-list' aria-label='Source freshness'>"]
    if not sources:
        out.append("<p class='empty'>No source health records in this snapshot.</p>")
    for name, value in sorted(sources.items()):
        value = _as_dict(value)
        state, state_label = _state(value.get("status"))
        age = _age_seconds(value.get("age_seconds"))
        error = value.get("error_code")
        out.append("<article class='source-card'><div class='source-head'>"
                   "<span class='source-name'>{}</span><span class='state {}'>{}</span>"
                   "</div><div class='source-meta'>Observed: {} · {}</div>".format(
                       _e(_display(name, "unknown", 80)), state, state_label,
                       _e(_display(value.get("observed_at"), "unknown", 48)), _e(age)))
        if error:
            out.append("<div class='source-meta'>Source error: <code>{}</code></div>".format(
                _e(_display(error, "unknown", 80))))
        if value.get("last_success_at"):
            out.append("<div class='source-meta'>Last successful collection: {}</div>".format(
                _e(_display(value.get("last_success_at"), "unknown", 48))))
        out.append("</article>")
    out.append("</div>")
    return "".join(out)


def _fmt_rate(rate):
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return "Unavailable"
    return "{:.1f}%".format(rate * 100)


def _render_sankey_svg(sankey):
    left = sankey.get("left", [])
    right = sankey.get("right", [])
    flows = sankey.get("flows", [])
    if not flows:
        return "<p class='empty'>No canonical applications are available for this source-to-current-status view.</p>"
    width = 940
    row_gap = 42
    height = max(130, 34 + max(len(left), len(right)) * row_gap)
    left_y = {label: 26 + index * row_gap for index, label in enumerate(left)}
    right_y = {label: 26 + index * row_gap for index, label in enumerate(right)}
    out = ["<svg role='img' aria-labelledby='flow-title flow-desc' viewBox='0 0 {} {}' "
           "xmlns='http://www.w3.org/2000/svg'><title id='flow-title'>Posting source to current application status</title>"
           "<desc id='flow-desc'>Counts of canonical applications grouped by recorded source and their current status. This is not transition history.</desc>".format(width, height)]
    for index, flow in enumerate(flows):
        source, status, count = flow["from"], flow["to"], flow["count"]
        y1, y2 = left_y[source], right_y[status]
        color = SOURCE_COLORS[index % len(SOURCE_COLORS)]
        stroke = min(18, max(2, 2 + count * 3))
        out.append("<path class='flow' d='M 245 {} C 390 {}, 550 {}, 700 {}' "
                   "stroke='{}' stroke-width='{}'><title>{} to {}: {}</title></path>".format(
                       y1, y1, y2, y2, color, stroke, _e(source), _e(status), count))
    for label, y in left_y.items():
        out.append("<rect class='node' x='12' y='{}' width='232' height='28' rx='6'/><text x='22' y='{}'>{}</text>".format(
            y - 14, y + 5, _e(label)))
    for label, y in right_y.items():
        out.append("<rect class='node' x='700' y='{}' width='228' height='28' rx='6'/><text x='710' y='{}'>{}</text>".format(
            y - 14, y + 5, _e(label)))
    out.append("</svg>")
    return "".join(out)


def _render_applications(applications, sankey, sources):
    sources = _as_dict(sources)
    app_source_status = {
        name: _as_dict(sources.get(name)).get("status", "unavailable")
        for name in ("tracker", "queue")
    }
    grouped = {status: [] for status in APPLICATION_STATUS_ORDER}
    for app in applications:
        grouped.setdefault(app.get("status", "Unknown / unmatched"), []).append(app)
    out = []
    if all(status == "unavailable" for status in app_source_status.values()):
        out.append("<div class='empty' role='status'><strong>Application data unavailable.</strong> Tracker and queue are unavailable; empty status columns do not mean zero applications.</div>")
    elif any(status != "fresh" for status in app_source_status.values()):
        states = ", ".join("{}: {}".format(name, status)
                            for name, status in sorted(app_source_status.items()))
        out.append("<div class='empty' role='status'><strong>Application source coverage is incomplete.</strong> {}. Rows below are last-good cached facts when available; read each source error on Today.</div>".format(
            _e(states)))
    out.append("<div class='board'>")
    for status in APPLICATION_STATUS_ORDER:
        cards = grouped.get(status, [])
        out.append("<section class='board-column' aria-label='{} applications'>"
                   "<h3><span>{}</span><span class='count'>{}</span></h3>".format(
                       _e(status), _e(status), len(cards)))
        if not cards:
            out.append("<p class='empty'>No canonical application rows in this status.</p>")
        else:
            out.append("<div class='application-list'>")
            for app in cards:
                company = _e(app.get("company", "Company unknown"))
                role = _e(app.get("role", "Role unknown"))
                links = []
                if app.get("link"):
                    links.append("<a href='{}' rel='noreferrer'>Posting</a>".format(
                        _e(app["link"])))
                if app.get("pr"):
                    links.append("<a href='{}' rel='noreferrer'>PR</a>".format(_e(app["pr"])))
                out.append("<article class='application-card'><div class='card-top'>"
                           "<div><div class='card-title'>{}</div><div class='role'>{}</div></div>"
                           "<span class='badge'>{}</span></div>".format(
                               company, role, _e(app.get("status"))))
                out.append("<div class='details'>Source: {} · {}</div>".format(
                    _e(app.get("source", "Unknown source")), _e(app.get("age_label", "No date recorded"))))
                if links:
                    out.append("<div class='link-list'>{}</div>".format("".join(links)))
                out.append("<details><summary>Timeline and dated evidence</summary><ul class='timeline'>")
                for item in app.get("timeline", []):
                    out.append("<li>{}</li>".format(_e(item)))
                out.append("</ul></details></article>")
            out.append("</div>")
        out.append("</section>")
    out.append("</div>")
    out.extend(["<section class='panel chart'><h3>Posting source → current status</h3>",
                "<p class='caveat'>Each canonical application is counted once. This diagram shows current status by recorded posting source; it is not a history of stage transitions.</p>",
                _render_sankey_svg(sankey)])
    if sankey.get("flows"):
        out.append("<div class='data-table-wrap'><table><caption>Source-to-current-status counts</caption><thead><tr><th scope='col'>Posting source</th><th scope='col'>Current status</th><th scope='col'>Applications</th></tr></thead><tbody>")
        for flow in sankey["flows"]:
            out.append("<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _e(flow["from"]), _e(flow["to"]), flow["count"]))
        out.append("</tbody></table></div>")
    out.append("</section>")
    return "".join(out)


def _render_today(report, sources, compact):
    goal = _as_dict(report.get("submission_goal"))
    buffer = _as_dict(report.get("ready_buffer"))
    attempts = _as_dict(report.get("attempts"))
    sync = _as_dict(report.get("tracker_sync"))
    gaps = _as_dict(report.get("snapshot_gaps"))
    blockers = _as_dict(report.get("blockers"))
    out = ["<div class='metrics'>",
           "<article class='metric'><span class='metric-label'>Daily submission goal · {}</span>"
           "<span class='metric-value'>{} / {}</span><span class='meta'>Coverage: {}</span></article>".format(
               _e(report.get("timezone", "unknown")), _e(_count(goal.get("confirmed"))),
               _e(_count(goal.get("target"))), _e(_as_dict(goal.get("submissions")).get("coverage", "unknown"))),
           "<article class='metric'><span class='metric-label'>Ready buffer</span>"
           "<span class='metric-value'>{} / {}</span><span class='meta'>Gap: {}</span></article>".format(
               _e(_count(buffer.get("queued"))), _e(_count(buffer.get("target"))),
               _e(_count(buffer.get("gap")))),
           "<article class='metric'><span class='metric-label'>Attention</span>"
           "<span class='metric-value'>Review signals</span><span class='meta'>Stale in progress: {} · outcome unknown: {} · sync pending: {} · snapshot gaps: {}</span></article>".format(
               _e(_count(attempts.get("stale_in_progress"))),
               _e(_count(attempts.get("outcome_unknown"))),
               _e(_count(sync.get("pending_or_error"))),
               _e(_count(gaps.get("total"))))]
    out.append("</div>")
    by_code = blockers.get("by_code")
    if by_code:
        out.append("<section class='panel'><h3>Structured blockers</h3><ul class='reason-list'>")
        for code, count in sorted(by_code.items()):
            out.append("<li><code>{}</code> · {}</li>".format(_e(code), _e(count)))
        out.append("</ul><p class='meta'>Unclassified blockers: {}</p></section>".format(
            _e(_count(blockers.get("unclassified")))))
    out.append("<section class='panel'><h3>Source freshness</h3>{}</section>".format(
        _render_sources(sources)))
    out.append("<section class='panel'><h3>Shared compact summary</h3><pre class='compact'>{}</pre></section>".format(
        _e(compact)))
    return "".join(out)


def _render_runs(trace_report):
    source = _as_dict(trace_report.get("source"))
    state, state_label = _state(source.get("status"))
    runs = trace_report.get("runs", [])
    out = ["<p class='intro'>Run records are available only when the trace source has collected them. Tool arguments and output are not included; cost remains unavailable.</p>",
           "<p><span class='state {}'>Trace source: {}</span>{}</p>".format(
               state, _e(state_label), " · source error: <code>{}</code>".format(_e(source.get("error_code")))
               if source.get("error_code") else "")]
    if not runs:
        if source.get("status") == "unavailable":
            out.append("<p class='empty'>Run history is unavailable in this snapshot.</p>")
        else:
            out.append("<p class='empty'>No run records were observed in this snapshot.</p>")
        return "".join(out)
    out.append("<div class='run-list'>")
    for run in runs:
        if not isinstance(run, dict):
            continue
        job = _display(run.get("job_id"), "Unknown job", 100)
        run_id = _display(run.get("run_id"), "Unknown run", 100)
        out.append("<article class='run-card'><h3>{} · {}</h3>".format(_e(job), _e(run_id)))
        fields = (("Execution", "execution_result"), ("Completion", "completion_status"),
                  ("Business result", "business_result"), ("Application join", "application_join_status"),
                  ("Artifact join", "artifact_join_status"), ("Delivery", "delivery_status"),
                  ("Tool trace", "tool_trace_status"))
        out.append("<dl class='key-value'>")
        for label, key in fields:
            out.append("<dt>{}</dt><dd>{}</dd>".format(
                _e(label), _e(_display(run.get(key), "unknown", 100))))
        out.append("</dl>")
        attention = run.get("attention_codes") or []
        out.append("<p class='details'>Attention codes: {}</p>".format(
            _e(", ".join(_display(code, "unknown", 80) for code in attention) or "none")))
        tools = run.get("tool_events") or []
        if tools:
            out.append("<details><summary>Redacted tool details ({})</summary><ul class='timeline'>".format(len(tools)))
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                out.append("<li><strong>{}</strong> · start {} · end {} · duration {} ms · result {} · retry {}</li>".format(
                    _e(_display(tool.get("name"), "unknown tool", 100)),
                    _e(_display(tool.get("started_at") or tool.get("at"), "unknown", 48)),
                    _e(_display(tool.get("finished_at"), "unknown", 48)),
                    _e(_count(tool.get("duration_ms"))),
                    _e(_display(tool.get("result"), "unknown", 48)),
                    _e(_display(tool.get("retry_context"), "unknown", 80))))
            out.append("</ul></details>")
        else:
            out.append("<p class='meta'>No redacted tool details are available for this run.</p>")
        out.append("</article>")
    out.extend(["</div>", "<p class='caveat'>Usage scopes may overlap. Cost is unavailable and is not estimated.</p>"])
    return "".join(out)


def _render_quality(quality):
    unique = quality.get("unique_artifact_heads", 0)
    first = _as_dict(quality.get("first_pass"))
    out = ["<p class='intro'>First-pass acceptance is counted once per unique artifact head. Model and explicit owner decisions use the same head denominator and remain separate.</p><div class='quality-grid'>"]
    for label, key in (("Model review", "model"), ("Owner review", "owner")):
        values = _as_dict(first.get(key))
        accepted = values.get("accepted", 0)
        out.append("<article class='cohort'><h3>{}</h3><div class='metric-value'>{} / {}</div>"
                   "<dl class='key-value'><dt>Accepted</dt><dd>{}</dd><dt>Not accepted</dt><dd>{}</dd>"
                   "<dt>Unknown</dt><dd>{}</dd><dt>Acceptance rate</dt><dd>{}</dd></dl></article>".format(
                       _e(label), _e(_count(accepted)), _e(_count(unique)),
                       _e(_count(accepted)), _e(_count(values.get("not_accepted"))),
                       _e(_count(values.get("unknown"))), _e(_fmt_rate(accepted / unique if unique else None))))
    out.append("</div>")
    provenance = Counter(quality.get("provenance_status_counts", {}))
    out.append("<section class='panel'><h3>Unknowns and provenance</h3><p>Review/preflight records without a usable artifact head: {}</p><p>Unique heads with provenance status unknown: {}</p>".format(
        _e(_count(quality.get("unknown_head_records"))),
        _e(provenance.get("unknown", 0))))
    if provenance:
        out.append("<ul class='reason-list'>")
        for status, count in sorted(provenance.items()):
            out.append("<li>Provenance <code>{}</code> · {}</li>".format(_e(status), count))
        out.append("</ul>")
    out.append("</section><section class='panel'><h3>Structured rework reasons</h3>")
    reasons = quality.get("rework_by_code", {})
    if reasons:
        out.append("<ul class='reason-list'>")
        for code, count in sorted(reasons.items()):
            out.append("<li><code>{}</code> · {}</li>".format(_e(code), _e(count)))
        out.append("</ul>")
    else:
        out.append("<p class='empty'>No structured rework reason codes are recorded.</p>")
    out.append("</section>")
    interpretation = quality.get("interpretation")
    if interpretation:
        out.append("<p class='caveat'>{}</p>".format(_e(interpretation)))
    return "".join(out)


def _render_funnel(report):
    out = ["<p class='intro'>Outcome types are independent observations, not a sequence of workflow stages. Only mature cohorts are included; missing evidence is not counted as a confirmed non-response.</p><div class='cohorts'>"]
    for cohort in report.get("mature_cohorts", []):
        window = cohort.get("window_days", "?")
        coverage = _as_dict(cohort.get("coverage"))
        out.append("<article class='cohort'><h3>{}-day mature cohort</h3><p>Eligible applications: <strong>{}</strong></p><p>Submission cutoff: {} · event source: {}</p>".format(
            _e(window), _e(_count(cohort.get("eligible_denominator"))),
            _e(cohort.get("cutoff_date", "unknown")),
            _e(cohort.get("event_source_status", "unavailable"))))
        if coverage:
            out.append("<p>Event coverage: <strong>{}</strong></p>".format(
                _e(coverage.get("status", "unknown"))))
        else:
            out.append("<p>Event coverage: <strong>Unavailable</strong></p>")
        out.append("<dl class='key-value'>")
        numerator = _as_dict(cohort.get("observed_numerator"))
        rates = _as_dict(cohort.get("outcome_rates"))
        for name in ("callback", "oa", "interview", "offer", "other"):
            out.append("<dt>{}</dt><dd>{} · {}</dd>".format(
                _e(name.upper() if name == "oa" else name.capitalize()),
                _e(_count(numerator.get(name))), _e(_fmt_rate(rates.get(name)))))
        out.append("</dl><details><summary>Coverage and exclusion counts</summary>")
        exclusions = _as_dict(cohort.get("exclusions"))
        if exclusions:
            out.append("<dl class='key-value'>")
            for key, value in sorted(exclusions.items()):
                out.append("<dt>{}</dt><dd>{}</dd>".format(_e(key.replace("_", " ")),
                                                             _e(_count(value))))
            out.append("</dl>")
        if coverage.get("ledger"):
            ledger = coverage["ledger"]
            out.append("<p>Ledger coverage: complete={} · observed={} · last scan={}</p>".format(
                _e(_count(ledger.get("complete"))),
                _e(_display(ledger.get("observed_at"), "unknown", 48)),
                _e(_display(ledger.get("last_scan_at"), "unknown", 48))))
        out.append("</details><p class='caveat'>{}</p></article>".format(
            _e(cohort.get("coverage_caveat", "Coverage unknown."))))
    out.append("</div>")
    out.append("<section class='panel'><h3>Data quality notes</h3><p>Identity conflicts: {}</p><p>{}</p><p>{}</p></section>".format(
        _e(_count(_as_dict(report.get("identity_diagnostics")).get("identity_conflict_count"))),
        _e(report.get("human_callback_rule", "Human callback origin is not established.")),
        _e(report.get("grouping_caveat", "Grouped rates are descriptive only."))))
    return "".join(out)


def render_snapshot_html(snapshot, data=None):
    """Render all views from a cached snapshot as one self-contained HTML file."""
    data = data or build_snapshot_data(snapshot)
    snapshot_id = _display(snapshot.get("snapshot_id"), "unknown", 128)
    snapshot_date = _display(snapshot.get("as_of"), "unknown", 48)
    generated = _display(snapshot.get("generated_at"), "unknown", 48)
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<meta name='color-scheme' content='light dark'>",
           "<meta name='referrer' content='no-referrer'>",
           "<title>Private jobs dashboard</title><style>", DASHBOARD_CSS,
           "</style></head><body><a class='skip' href='#main'>Skip to dashboard content</a><div class='shell'>",
           "<header class='masthead'><h1>Private jobs dashboard</h1><div class='mast-meta'>",
           "<span>Snapshot as of {}</span><span>Collected {}</span>".format(
               _e(snapshot_date), _e(generated)),
           "<span class='snapshot-id'>Snapshot ID: <code>{}</code></span></div></header>".format(
               _e(snapshot_id)),
           "<nav class='nav' aria-label='Dashboard views'><a href='#today'>Today</a><a href='#applications'>Applications</a><a href='#runs'>Runs</a><a href='#quality'>Tailoring quality</a><a href='#funnel'>Hiring funnel</a></nav>",
           "<main id='main'>",
           "<section class='view' id='today' aria-labelledby='today-heading'><h2 id='today-heading'>Today</h2>",
           _render_today(data["today"], data["sources"], data["compact"]),
           "</section><section class='view' id='applications' aria-labelledby='applications-heading'><h2 id='applications-heading'>Applications</h2><p class='intro'>Read-only status board. Counts include canonical tracker/queue rows in this snapshot; discovered postings without a canonical row are not in the snapshot and unmatched rows never default to Applied. Status age uses a known stage start when present; otherwise the displayed label names the date basis. Timelines contain dated submission claims and recorded events only.</p>",
           _render_applications(data["applications"], data["sankey"], data["sources"]),
           "</section><section class='view' id='runs' aria-labelledby='runs-heading'><h2 id='runs-heading'>Runs</h2>",
           _render_runs(data["runs"]),
           "</section><section class='view' id='quality' aria-labelledby='quality-heading'><h2 id='quality-heading'>Tailoring quality</h2>",
           _render_quality(data["quality"]),
           "</section><section class='view' id='funnel' aria-labelledby='funnel-heading'><h2 id='funnel-heading'>Hiring funnel</h2>",
           _render_funnel(data["funnel"]),
           "</section></main><footer>Private local dashboard · read-only snapshot · no source collection is triggered by viewing this page.</footer>",
           "<script type='application/json' id='dashboard-state'>",
           _safe_json_for_script({"snapshot_id": snapshot.get("snapshot_id"),
                                  "as_of": snapshot.get("as_of")}),
           "</script></div></body></html>"]
    return "".join(out)


def render_unavailable_html():
    """Render an explicit no-cache state without exposing a local path/error."""
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Dashboard unavailable</title><style>"
            "body{margin:0;padding:24px;background:#f8fafc;color:#1e293b;"
            "font:16px/1.55 system-ui,sans-serif}main{max-width:48rem;margin:auto}"
            "</style></head><body><main><h1>Cached dashboard unavailable</h1>"
            "<p>No committed snapshot could be read. The page did not contact or scan any source.</p>"
            "<p>An operator can run the explicit projection collection command after checking private runtime configuration.</p>"
            "</main></body></html>")


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(
        description="Render the private dashboard from the shared cached projection.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="cached projection database (read-only; collect separately)")
    ap.add_argument("--repo", default=REPO_DIR,
                    help="legacy source path; used only with --legacy-collect")
    ap.add_argument("--out", default=OUT_PATH,
                    help="output HTML path (env JOBSEARCH_DASHBOARD_OUT)")
    ap.add_argument("--json", action="store_true",
                    help="print the dashboard view model instead of writing HTML")
    ap.add_argument("--queue-cmd", default=None,
                    help="legacy queue command; used only with --legacy-collect")
    ap.add_argument("--legacy-collect", action="store_true",
                    help="explicitly run the old source collector (may call gh)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)  # noqa: E731
    if args.legacy_collect:
        queue_cmd = args.queue_cmd.split() if args.queue_cmd else None
        data = build_data(args.repo, queue_cmd, log=log)
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))
            return 0
        html_doc = render_html(data, generated=data.get("generated"))
    else:
        import projection
        try:
            snapshot = projection.read_snapshot(args.db)
            data = build_snapshot_data(snapshot)
        except (projection.ProjectionError, OSError, ValueError, KeyError, TypeError):
            print("dashboard: cached snapshot unavailable or invalid; no source collection was run",
                  file=sys.stderr)
            if args.json:
                return 2
            data = {"snapshot_id": "unavailable"}
            html_doc = render_unavailable_html()
        else:
            if args.json:
                print(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))
                return 0
            html_doc = render_snapshot_html(snapshot, data=data)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    os.replace(tmp, args.out)
    log("dashboard: wrote {} ({} bytes) snapshot={}".format(
        args.out, len(html_doc.encode("utf-8")), data.get("snapshot_id", "legacy")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
