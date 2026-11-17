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
import hashlib
import html
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from urllib.parse import parse_qs, urlsplit

import summary as s
from config import default_repo_dir, default_runtime_path, path_setting
import dashboard_shell
import application_explorer as explorer

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
RUNS_PAGE_SIZE = 20
RUNS_MAX_PAGE = 50

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
  color-scheme: light;
  --bg: #f8fafc; --surface: #fff; --surface-muted: #e9eff8;
  --text: #1e293b; --muted: #475569; --border: #cbd5e1;
  --primary: #2563eb; --success: #166534; --warning: #9a3412;
  --danger: #b42318; --focus: #1d4ed8;
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
.shell { width: min(100%, 1440px); margin-inline: auto; padding: 16px 12px 40px; }
.masthead { border-bottom: 1px solid var(--border); padding: 8px 0 18px; }
h1 { margin: 0; font-size: clamp(1.7rem, 4vw, 2.35rem); line-height: 1.2; letter-spacing: -.025em; }
h2 { margin: 0 0 12px; font-size: 1.45rem; line-height: 1.3; }
h3 { margin: 0 0 8px; font-size: 1.08rem; line-height: 1.35; }
p { margin: 8px 0; }
.snapshot-asof { margin: 10px 0 0; color: var(--muted); }
.provenance dl { display: grid; grid-template-columns: minmax(0, 10rem) minmax(0, 1fr); gap: 4px 12px; margin: 8px 0; }
.provenance dl div { display: contents; }
.provenance dt { color: var(--muted); }
.provenance dd { margin: 0; overflow-wrap: anywhere; font: .9rem/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
.workspace { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.nav { padding: 12px 0; border-bottom: 1px solid var(--border); }
.nav ul { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; padding: 0; list-style: none; }
.nav li { min-width: 0; }
.nav a { display: flex; align-items: center; min-width: 0; min-height: 44px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-weight: 600; text-decoration: none; overflow-wrap: anywhere; }
.nav a:hover { background: var(--surface-muted); }
.nav a[aria-current='page'] { border-color: var(--primary); background: var(--primary); color: #fff; }
.view { padding: 26px 0 10px; scroll-margin-top: 12px; }
main { min-width: 0; padding-top: 22px; }
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
.explorer-views, .pagination, .explorer-filters { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin: 12px 0; }
.explorer-views a, .pagination a, .load-more, .detail-link, .explorer-filters button { min-height: 44px; display: inline-flex; align-items: center; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
.explorer-views a[aria-current='page'] { background: var(--primary); color: white; }
.explorer-filters label { display: grid; gap: 4px; font-weight: 600; }
.explorer-filters input, .explorer-filters select { min-height: 44px; max-width: 100%; padding: 8px; border: 1px solid var(--border); border-radius: 6px; font: inherit; }
.explorer-filters input[type='search'] { width: min(28rem, 80vw); }
.explorer-filters button { color: var(--text); font: inherit; cursor: pointer; }
.phone-cards { display: none; }
.result-count { color: var(--muted); }
.board { align-items: start; }
.board-column { align-self: start; }
.load-more { margin-top: 10px; }
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
.key-value { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 5px 12px; margin: 10px 0; }
.key-value dt { color: var(--muted); overflow-wrap: anywhere; }
.key-value dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.reason-list { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; list-style: none; }
.reason-list li { padding: 5px 9px; border: 1px solid var(--border); border-radius: 8px; overflow-wrap: anywhere; }
details { margin-top: 10px; }
summary { min-height: 44px; display: list-item; padding: 10px 0; cursor: pointer; font-weight: 650; }
.caveat { color: var(--muted); font-size: .94rem; }
footer { margin-top: 32px; padding-top: 14px; border-top: 1px solid var(--border); color: var(--muted); font-size: .92rem; }
@media (min-width: 48rem) {
  .shell { padding: 28px 24px 56px; }
  .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .source-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .board { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .cohorts, .quality-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 47.999rem) {
  .explorer-table { display: none; }
  .phone-cards { display: grid; gap: 10px; }
}
@media (min-width: 64rem) {
  .shell { padding-inline: 32px; }
  .workspace { grid-template-columns: 220px minmax(0, 1fr); gap: 28px; align-items: start; }
  .nav { position: sticky; top: 16px; padding: 24px 0; border: 0; }
  .nav ul { grid-template-columns: minmax(0, 1fr); }
  main { padding-top: 24px; }
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
    """Build compact rows; detailed timelines are resolved only on detail requests."""
    facts = _as_dict(snapshot.get("facts"))
    raw_apps = [row for row in facts.get("applications", []) if isinstance(row, dict)]
    try:
        today = dt.date.fromisoformat(snapshot.get("as_of"))
    except (TypeError, ValueError):
        today = None
    result = []
    for app in raw_apps:
        row = _as_dict(app.get("row"))
        status = _status_for_row(row)
        stage_since = row.get("stage_since")
        days, age_from = card_age(row, today) if today else (None, None)
        pr = _safe_http_url(row.get("pr"))
        if not pr and str(row.get("pr_number", "")).isdigit():
            pr = "https://github.com/{}/pull/{}".format(s.GH_REPO_DEFAULT, row["pr_number"])
        result.append({
            "company": _display(row.get("company"), "Company unknown"),
            "role": _display(row.get("role"), "Role unknown"),
            "source": _display(row.get("source"), "Unknown source", 120),
            "application_key": explorer.opaque_key(app.get("application_id")),
            "status": status,
            "stage_since": _display(stage_since, "", 40) if stage_since else None,
            "applied_date": _display(row.get("applied_date"), "", 40) if row.get("applied_date") else None,
            "days": days, "age_from": age_from,
            "age_label": _age_label({"days": days, "age_from": age_from,
                                      "stage_since": stage_since, "status": status}),
            "link": _safe_http_url(row.get("link")), "pr": pr,
        })
    known_ids = {app.get("application_id") for app in raw_apps}
    for attempt in facts.get("attempts", []):
        if not isinstance(attempt, dict) or attempt.get("application_id") in known_ids:
            continue
        code = attempt.get("blocker_code")
        result.append({
            "company": _display(attempt.get("company"), "Company unknown"),
            "role": _display(attempt.get("role"), "Role unknown"),
            "source": _display(attempt.get("source"), "Unknown source", 120),
            "application_key": None, "status": "Unknown / unmatched",
            "stage_since": None, "applied_date": None, "days": None, "age_from": None,
            "age_label": "No matched application or stage date",
            "link": _safe_http_url(attempt.get("link")),
            "pr": _safe_http_url(attempt.get("pr")),
            "attempt_summary": "Unmatched queue attempt · {}{}".format(
                _display(attempt.get("status"), "unknown", 48),
                " · blocker " + _display(code, "", 80) if code else ""),
        })
    return sorted(result, key=lambda item: (
        APPLICATION_STATUS_ORDER.index(item["status"])
        if item["status"] in APPLICATION_STATUS_ORDER else len(APPLICATION_STATUS_ORDER),
        item["company"].casefold(), item["role"].casefold()))


def _application_detail(snapshot, key):
    """Resolve exactly one canonical application from its opaque route key."""
    facts = _as_dict(snapshot.get("facts"))
    matches = [app for app in facts.get("applications", [])
               if isinstance(app, dict)
               and explorer.opaque_key(app.get("application_id")) == key]
    if len(matches) != 1:
        return "ambiguous" if len(matches) > 1 else None
    app = matches[0]
    app_id = app.get("application_id")
    row = _as_dict(app.get("row"))
    aliases = _as_dict(facts.get("application_aliases"))
    conflicts = set(facts.get("conflicted_application_aliases", []))
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
    status = _status_for_row(row)
    stage_since = row.get("stage_since")
    timeline.append("Current status {}{}".format(
        status, " since " + _display(stage_since, "", 40) if stage_since else " · stage date unknown"))
    for event in facts.get("events", []):
        if not isinstance(event, dict):
            continue
        event_id = event.get("application_id")
        if isinstance(event_id, str) and event_id not in conflicts and aliases.get(event_id, event_id) == app_id:
            timeline.append("{} event · {} · {}".format(
                _display(event.get("occurred_at"), "unknown date", 48),
                "verified" if event.get("verification") == "verified" else "unverified",
                _display(event.get("event_type"), "unknown event", 80)))
    for attempt in facts.get("attempts", []):
        if isinstance(attempt, dict) and attempt.get("application_id") == app_id:
            code = attempt.get("blocker_code")
            timeline.append("Queue attempt: {}{}".format(
                _display(attempt.get("status"), "unknown", 48),
                " · blocker " + _display(code, "", 80) if code else ""))
    pr = _safe_http_url(row.get("pr"))
    if not pr and str(row.get("pr_number", "")).isdigit():
        pr = "https://github.com/{}/pull/{}".format(s.GH_REPO_DEFAULT, row["pr_number"])
    return {"company": _display(row.get("company"), "Company unknown"),
            "role": _display(row.get("role"), "Role unknown"), "status": status,
            "applied_date": _display(row.get("applied_date"), "", 40) if row.get("applied_date") else None,
            "stage_since": _display(stage_since, "", 40) if stage_since else None,
            "source": _display(row.get("source"), "Unknown source", 120),
            "link": _safe_http_url(row.get("link")), "pr": pr, "timeline": timeline,
            "next_action": _display(row.get("next_action"), "No next action recorded", 200),
            "provenance": "Canonical application record · source {}".format(
                _display(app.get("source"), "unknown", 80))}


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
    heads = [row for row in quality_source.get("records", []) if isinstance(row, dict)]
    quality["potential_mistailor_heuristic"] = sum(
        row.get("model_acceptance") == "accepted"
        and row.get("owner_acceptance") == "not_accepted"
        for row in heads)
    quality["mistailor_unknown_owner"] = sum(
        row.get("model_acceptance") == "accepted"
        and row.get("owner_acceptance") == "unknown"
        for row in heads)
    quality["source_status"] = {
        name: _as_dict(snapshot.get("sources", {}).get(name)).get("status", "unknown")
        for name in ("reviews", "preflight")}
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


def parse_runs_query(query=""):
    """Parse a deliberately small, bounded Runs page query."""
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=1)
    except (ValueError, TypeError):
        raise ValueError("invalid runs query")
    if not values:
        return 1
    if set(values) != {"page"} or len(values["page"]) != 1:
        raise ValueError("invalid runs query")
    raw = values["page"][0]
    if not raw.isdigit() or len(raw) > 2:
        raise ValueError("invalid runs page")
    page = int(raw)
    if page < 1 or page > RUNS_MAX_PAGE:
        raise ValueError("invalid runs page")
    return page


def run_detail_key(run):
    """Return an opaque stable key only when a run has both supported IDs."""
    job_id = run.get("job_id")
    run_id = run.get("run_id")
    if (not isinstance(job_id, str) or not job_id or len(job_id) > 100
            or not isinstance(run_id, str) or not run_id or len(run_id) > 100):
        return None
    return hashlib.sha256((job_id + "\0" + run_id).encode("utf-8")).hexdigest()


def _run_priority(run):
    execution = run.get("execution_result")
    completion = run.get("completion_status")
    if execution in ("failed", "interrupted") or completion == "failed":
        return 0
    if (execution in (None, "unknown") or completion in (None, "unknown")
            or run.get("tool_trace_status") in ("incomplete", "missing", "unsupported_schema")):
        return 1
    if execution == "succeeded" and completion == "succeeded":
        return 2
    return 3


def _ordered_runs(runs):
    return sorted((run for run in runs if isinstance(run, dict)), key=lambda run: (
        _run_priority(run), str(run.get("finished_at") or ""),
        str(run.get("job_id") or ""), str(run.get("run_id") or "")))


def _run_detail(snapshot, key):
    facts = _as_dict(_as_dict(snapshot.get("facts")).get("traces"))
    matches = [run for run in facts.get("runs", [])
               if isinstance(run, dict) and run_detail_key(run) == key]
    if len(matches) > 1:
        return "ambiguous"
    return matches[0] if matches else None


def _render_sources(sources):
    out = ["<div class='source-list' aria-label='Source freshness'>"]
    if not sources:
        out.append("<p class='empty'>No source health records in this snapshot.</p>")
    for name, value in sorted(sources.items()):
        value = _as_dict(value)
        state, state_label = _state(value.get("status"))
        age = _age_seconds(value.get("age_seconds"))
        error = value.get("error_code")
        coverage = value.get("coverage_status")
        if error and state == "stale":
            impact = "The latest collection failed; last-good cached facts may be out of date."
            next_step = "Check the collection result, then refresh the cache when safe."
        elif error or state == "unavailable":
            impact = "No usable cached facts are available from this source."
            next_step = "Check the source configuration and collection result before relying on this data."
        elif coverage in ("partial", "incomplete"):
            impact = "Only part of this source was collected, so some records may be missing."
            next_step = "Review the coverage reasons and resolve the collection gap."
        elif state == "stale":
            impact = "The last observed data is older than its freshness window."
            next_step = "Review the source and refresh its cached data when appropriate."
        elif state == "fresh":
            impact = "The latest cached source data is within its freshness window."
            next_step = "No source action is indicated by this snapshot."
        else:
            impact = "Source freshness is unknown."
            next_step = "Check collection metadata before relying on its coverage."
        out.append("<article class='source-card'><div class='source-head'>"
                   "<span class='source-name'>{}</span><span class='state {}'>{}</span>"
                   "</div><div class='source-meta'>Last observation: {}</div>".format(
                       _e(_display(name, "unknown", 80)), state, state_label,
                       _e(age)))
        out.append("<p>{}</p><p><strong>Next step:</strong> {}</p>".format(
            _e(impact), _e(next_step)))
        out.append("<details><summary>Source provenance</summary><dl class='key-value'>")
        for label, key in (("Observed at", "observed_at"),
                           ("Collected", "collected_at"),
                           ("Last successful collection", "last_success_at"),
                           ("Freshness window (seconds)", "freshness_seconds"),
                           ("Source error code", "error_code"),
                           ("Coverage status", "coverage_status"),
                           ("Coverage reasons", "coverage_reasons"),
                           ("Record count", "record_count"),
                           ("Quarantined count", "quarantined_count"),
                           ("Redacted count", "redacted_count")):
            source_value = value.get(key)
            if source_value is not None:
                if isinstance(source_value, (list, dict)):
                    source_value = json.dumps(source_value, ensure_ascii=True, sort_keys=True)
                out.append("<dt>{}</dt><dd>{}</dd>".format(
                    _e(label), _e(_display(source_value, "unknown", 180))))
        out.append("</dl></details>")
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


def _application_card(app, compact=False):
    title = _e(app.get("company", "Company unknown"))
    role = _e(app.get("role", "Role unknown"))
    if app.get("application_key"):
        title = "<a href='/jobs/applications/{}'>{}</a>".format(
            app["application_key"], title)
    out = ["<article class='application-card'><div class='card-top'><div class='card-title'>"
           "{}</div><span class='badge'>{}</span></div><div class='role'>{}</div>".format(
               title, _e(app.get("status", "Unknown / unmatched")), role)]
    meta = "Source: {}".format(_e(app.get("source", "Unknown source")))
    if app.get("applied_date"):
        meta += " · Submitted {}".format(_e(app["applied_date"]))
    out.append("<div class='details'>{}</div>".format(meta))
    if app.get("attempt_summary"):
        out.append("<div class='details'>{}</div>".format(_e(app["attempt_summary"])))
    if compact and app.get("application_key"):
        out.append("<a class='detail-link' href='/jobs/applications/{}'>Application details</a>".format(
            app["application_key"]))
    return "".join(out) + "</article>"


def _render_applications(applications, sankey, sources, state=None):
    state = state or explorer.parse_query("")
    sources = _as_dict(sources)
    app_source_status = {name: _as_dict(sources.get(name)).get("status", "unavailable")
                         for name in ("tracker", "queue")}
    rows = explorer.filtered_rows(applications, state)
    out = []
    if all(value == "unavailable" for value in app_source_status.values()):
        out.append("<div class='empty' role='status'><strong>Application data unavailable.</strong> Tracker and queue are unavailable; cached status columns do not imply zero applications.</div>")
    elif any(value != "fresh" for value in app_source_status.values()):
        states = ", ".join("{}: {}".format(name, value)
                            for name, value in sorted(app_source_status.items()))
        out.append("<div class='empty' role='status'><strong>Application source coverage is incomplete.</strong> {}. Rows show last-good cached facts when available.</div>".format(_e(states)))
    out.append("<nav class='explorer-views' aria-label='Application view'>")
    for view, label in (("table", "Table"), ("board", "Board")):
        current = " aria-current='page'" if state["view"] == view else ""
        out.append("<a{} href='{}'>{}</a>".format(current,
            _e(explorer.query_href(state, view=view, page=1)), label))
    out.append("</nav><form class='explorer-filters' method='get' action='/jobs/applications/'>")
    out.append("<label>Search applications <input type='search' name='q' maxlength='120' value='{}'></label>".format(_e(state["q"])))
    out.append("<label>Stage <select name='stage'>")
    for stage in ("all",) + explorer.STAGES:
        selected = " selected" if state["stage"] == stage else ""
        out.append("<option value='{}'{}>{}</option>".format(_e(stage), selected,
            "All stages" if stage == "all" else _e(stage)))
    out.append("</select></label><input type='hidden' name='view' value='{}'>".format(state["view"]))
    out.append("<input type='hidden' name='sort' value='{}'><input type='hidden' name='dir' value='{}'>".format(state["sort"], state["dir"]))
    out.append("<button type='submit'>Apply filters</button></form>")
    if not rows:
        out.append("<p class='empty' role='status'>No applications match these filters.</p>")
    elif state["view"] == "table":
        start = (state["page"] - 1) * explorer.PAGE_SIZE
        page_rows = rows[start:start + explorer.PAGE_SIZE]
        out.append("<p class='result-count' role='status'>{} matching applications · page {} of {}</p>".format(
            len(rows), min(state["page"], max(1, (len(rows) + explorer.PAGE_SIZE - 1) // explorer.PAGE_SIZE)),
            max(1, (len(rows) + explorer.PAGE_SIZE - 1) // explorer.PAGE_SIZE)))
        out.append("<div class='data-table-wrap explorer-table'><table><caption>Applications</caption><thead><tr>")
        for field, label in (("company", "Company"), ("role", "Role"), ("status", "Stage"), ("source", "Source"), ("applied_date", "Submitted")):
            direction = "desc" if state["sort"] == field and state["dir"] == "asc" else "asc"
            href = _e(explorer.query_href(state, sort=field, dir=direction, page=1))
            out.append("<th scope='col'><a href='{}'>{}</a></th>".format(href, label))
        out.append("</tr></thead><tbody>")
        for app in page_rows:
            company = _e(app.get("company", "Company unknown"))
            if app.get("application_key"):
                company = "<a href='/jobs/applications/{}'>{}</a>".format(app["application_key"], company)
            out.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                company, _e(app.get("role")), _e(app.get("status")),
                _e(app.get("source")), _e(app.get("applied_date") or "Unknown")))
        out.append("</tbody></table></div><div class='phone-cards'>")
        out.extend(_application_card(app, compact=True) for app in page_rows)
        out.append("</div>")
        pages = max(1, (len(rows) + explorer.PAGE_SIZE - 1) // explorer.PAGE_SIZE)
        out.append("<nav class='pagination' aria-label='Application pages'>")
        if state["page"] > 1:
            out.append("<a href='{}'>Previous page</a>".format(_e(explorer.query_href(state, page=state["page"] - 1))))
        if state["page"] < pages:
            out.append("<a href='{}'>Next page</a>".format(_e(explorer.query_href(state, page=state["page"] + 1))))
        out.append("</nav>")
    else:
        grouped = {stage: [] for stage in explorer.STAGES}
        for app in rows:
            grouped.setdefault(app.get("status", "Unknown / unmatched"), []).append(app)
        out.append("<div class='board'>")
        for stage in explorer.STAGES:
            lane = stage.lower().replace(" / unmatched", "").replace(" ", "-")
            cards = grouped.get(stage, [])
            limit = state["loads"].get(lane, explorer.LANE_SIZE)
            shown = cards[:limit]
            out.append("<section class='board-column' aria-label='{} applications'><h3><span>{}</span><span class='count'>{}</span></h3>".format(_e(stage), _e(stage), len(cards)))
            if not cards:
                out.append("<p class='empty'>No applications in this stage.</p>")
            else:
                out.append("<div class='application-list'>")
                out.extend(_application_card(app) for app in shown)
                out.append("</div>")
                if len(cards) > limit:
                    loads = dict(state["loads"])
                    loads[lane] = min(10000, limit + explorer.LANE_SIZE)
                    out.append("<a class='load-more' href='{}'>Load 10 more ({} remaining)</a>".format(
                        _e(explorer.query_href(state, loads=loads)), len(cards) - limit))
            out.append("</section>")
        out.append("</div>")
    out.extend(["<details class='panel chart'><summary>Posting source by current stage</summary>",
                "<p class='caveat'>Each canonical application is counted once. This diagram shows current status by recorded posting source; it is not a history of stage transitions.</p>",
                _render_sankey_svg(sankey)])
    out.append("</details>")
    return "".join(out)


def _render_application_detail(detail):
    if detail == "ambiguous":
        return "<div class='empty' role='status'>This application key matches multiple records. Open the application list and choose a specific record.</div>"
    if detail is None:
        return "<div class='empty' role='status'>Application details are unavailable for this record.</div>"
    out = ["<p><a href='/jobs/applications/'>Back to applications</a></p>",
           "<section class='panel detail-summary'><h3>{}</h3><p class='role'>{}</p>".format(
               _e(detail["company"]), _e(detail["role"])),
           "<p><strong>Stage:</strong> {}</p><h3>Submission evidence</h3><ul class='timeline'>".format(_e(detail["status"]))]
    for item in detail["timeline"]:
        out.append("<li>{}</li>".format(_e(item)))
    out.append("</ul><h3>Next action</h3><p>{}</p><div class='link-list'>".format(_e(detail["next_action"])))
    if detail.get("link"):
        out.append("<a href='{}' rel='noreferrer'>Posting</a>".format(_e(detail["link"])))
    if detail.get("pr"):
        out.append("<a href='{}' rel='noreferrer'>Review pull request</a>".format(_e(detail["pr"])))
    out.append("</div><details><summary>Evidence and provenance</summary><p>{}</p><p>Source: {}</p></details></section>".format(
        "Dated evidence above comes from recorded claims and events only.", _e(detail["provenance"])))
    return "".join(out)


def _render_today(report, compact):
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
    out.append("<details class='panel'><summary>Compact cached summary</summary><pre class='compact'>{}</pre></details>".format(
        _e(compact)))
    return "".join(out)


def _render_runs(trace_report, page=1):
    source = _as_dict(trace_report.get("source"))
    state, state_label = _state(source.get("status"))
    all_runs = _ordered_runs(trace_report.get("runs", []))
    page_count = max(1, (len(all_runs) + RUNS_PAGE_SIZE - 1) // RUNS_PAGE_SIZE)
    page = min(max(1, page), page_count)
    runs = all_runs[(page - 1) * RUNS_PAGE_SIZE:page * RUNS_PAGE_SIZE]
    out = ["<p class='intro'>Run records come from the cached trace source. Failures and incomplete runs appear first. Tool details contain only sanitized fields; unrecorded joins remain unknown and cost is unavailable.</p>",
           "<p><span class='state {}'>Trace source: {}</span></p>".format(
               state, _e(state_label))]
    if source.get("error_code") or trace_report.get("collection"):
        out.append("<details><summary>Trace collection metadata</summary><dl class='key-value'>")
        for label, value in (("Source error code", source.get("error_code")),
                             ("Collection status", _as_dict(trace_report.get("collection")).get("status"))):
            if value is not None:
                out.append("<dt>{}</dt><dd>{}</dd>".format(
                    _e(label), _e(_display(value, "unknown", 80))))
        out.append("</dl></details>")
    if not runs:
        if source.get("status") == "unavailable":
            out.append("<p class='empty' role='status'>Run history is unavailable in this snapshot.</p>")
        elif source.get("status") == "stale" or source.get("coverage_status") in ("partial", "incomplete"):
            out.append("<p class='empty' role='status'>No run records were observed, but trace coverage is stale or partial; this does not confirm that no runs occurred.</p>")
        else:
            out.append("<p class='empty' role='status'>No run records were observed in this snapshot.</p>")
        return "".join(out)
    out.append("<p class='result-count' role='status'>{} runs · page {} of {}</p>".format(
        len(all_runs), page, page_count))
    out.append("<div class='run-list'>")
    for run in runs:
        job = _display(run.get("job_id"), "Unknown job", 100)
        run_id = _display(run.get("run_id"), "Unknown run", 100)
        priority = _run_priority(run)
        label = {0: "Failure", 1: "Incomplete / unknown", 2: "Successful"}.get(priority, "Needs review")
        key = run_detail_key(run)
        heading = "{} · {}".format(_e(job), _e(run_id))
        if key:
            heading = "<a href='/jobs/runs/{}'>{}</a>".format(key, heading)
        out.append("<article class='run-card'><h3><span class='state'>{}</span> {}</h3>".format(
            _e(label), heading))
        out.append("<dl class='key-value'>")
        for label, key_name in (("Execution", "execution_result"),
                                ("Completion", "completion_status"),
                                ("Tool trace", "tool_trace_status")):
            out.append("<dt>{}</dt><dd>{}</dd>".format(
                _e(label), _e(_display(run.get(key_name), "unknown", 100))))
        out.append("</dl>")
        attention = run.get("attention_codes") or []
        out.append("<p class='details'>Attention codes: {}</p>".format(
            _e(", ".join(_display(code, "unknown", 80) for code in attention) or "none")))
        out.append("<p><strong>Business result:</strong> {} · <strong>Application join:</strong> {} · <strong>Artifact join:</strong> {} · <strong>Delivery:</strong> {}</p>".format(
            *[_e(_display(run.get(key_name), "unknown", 80)) for key_name in
              ("business_result", "application_join_status", "artifact_join_status", "delivery_status")]))
        out.append("</article>")
    out.append("</div><nav class='pagination' aria-label='Run pages'>")
    if page > 1:
        out.append("<a href='/jobs/runs/?page={}'>Previous page</a>".format(page - 1))
    if page < page_count:
        out.append("<a href='/jobs/runs/?page={}'>Next page</a>".format(page + 1))
    out.append("</nav><p class='caveat'>Tool timeline details open on each run page. Cost is unavailable and is not estimated.</p>")
    return "".join(out)


def _render_run_detail(run):
    if run == "ambiguous":
        return "<p class='empty' role='status'>This run key matches multiple records. Return to Runs and choose a specific run.</p>"
    if run is None:
        return "<p class='empty' role='status'>Run details are unavailable in this snapshot.</p>"
    out = ["<p><a href='/jobs/runs/'>Back to runs</a></p>",
           "<section class='panel run-card'><h3>{} · {}</h3><dl class='key-value'>".format(
               _e(_display(run.get("job_id"), "Unknown job", 100)),
               _e(_display(run.get("run_id"), "Unknown run", 100)))]
    for label, key_name in (("Execution", "execution_result"),
                            ("Completion", "completion_status"),
                            ("Business result", "business_result"),
                            ("Application join", "application_join_status"),
                            ("Artifact join", "artifact_join_status"),
                            ("Delivery", "delivery_status"),
                            ("Tool trace", "tool_trace_status"),
                            ("Run duration (ms)", "duration_ms"),
                            ("Retry context", "retry_context"),
                            ("Started", "started_at"), ("Finished", "finished_at")):
        value = run.get(key_name)
        if key_name.endswith("_ms") and (type(value) is not int or value < 0):
            value = "unknown"
        out.append("<dt>{}</dt><dd>{}</dd>".format(
            _e(label), _e(_display(value, "unknown", 100))))
    out.append("</dl><p>Attention codes: {}</p></section>".format(
        _e(", ".join(_display(code, "unknown", 80)
                      for code in run.get("attention_codes", [])) or "none")))
    tools = run.get("tool_events") or []
    if tools:
        out.append("<section class='panel'><h3>Redacted tool timeline</h3><ol class='timeline'>")
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            duration = tool.get("duration_ms")
            if type(duration) is not int or duration < 0:
                duration = "unknown"
            out.append("<li><strong>{}</strong><p class='meta'>Tool call: {}</p><dl class='key-value'><dt>Started</dt><dd>{}</dd><dt>Finished</dt><dd>{}</dd><dt>Duration (ms)</dt><dd>{}</dd><dt>Result</dt><dd>{}</dd><dt>Retry context</dt><dd>{}</dd></dl></li>".format(
                _e(_display(tool.get("name"), "Unknown tool", 100)),
                _e(_display(tool.get("tool_call_id"), "unknown", 100)),
                _e(_display(tool.get("started_at") or tool.get("at"), "unknown", 48)),
                _e(_display(tool.get("finished_at"), "unknown", 48)),
                _e(_display(duration, "unknown", 40)),
                _e(_display(tool.get("result"), "unknown", 48)),
                _e(_display(tool.get("retry_context"), "unknown", 80))))
        out.append("</ol></section>")
    else:
        out.append("<p class='empty'>No redacted tool details are available for this run.</p>")
    return "".join(out)


def _render_quality(quality):
    unique = quality.get("unique_artifact_heads", 0)
    first = _as_dict(quality.get("first_pass"))
    out = ["<p class='intro'>First-pass acceptance is counted once per unique artifact head. Model and explicit owner decisions use the same head denominator and remain separate.</p><div class='quality-grid'>"]
    source_status = _as_dict(quality.get("source_status"))
    if source_status and any(value != "fresh" for value in source_status.values()):
        out.append("<section class='panel' role='status'><h3>Review evidence source coverage is incomplete</h3><p>Counts describe records present in this snapshot; missing review history does not mean acceptance or rejection.</p><p>Review source: {} · preflight source: {}</p></section>".format(
            _e(_display(source_status.get("reviews"), "unknown", 40)),
            _e(_display(source_status.get("preflight"), "unknown", 40))))
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
    out.append("<section class='panel'><h3>Potential mistailors · heuristic</h3>"
               "<p>{} artifact heads have a recorded model acceptance and explicit owner nonacceptance. This is a review cue, not a verified tailoring defect or an application match.</p>"
               "<p>Model accepted, owner decision unknown: {}. Missing owner evidence stays unknown.</p></section>".format(
                   _e(_count(quality.get("potential_mistailor_heuristic"))),
                   _e(_count(quality.get("mistailor_unknown_owner")))))
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


def _render_home(data):
    return _render_today(data["today"], data["compact"])


def _render_applications_page(data):
    intro = ("<p class='intro'>Read-only status board. Counts include canonical tracker/queue rows "
             "in this snapshot; discovered postings without a canonical row are not in the snapshot "
             "and unmatched rows never default to Applied. Status age uses a known stage start when "
             "present; otherwise the displayed label names the date basis. Timelines contain dated "
             "submission claims and recorded events only.</p>")
    return intro + _render_applications(data["applications"], data["sankey"], data["sources"])


def _render_analytics(data):
    return _render_funnel(data["funnel"])


def _render_runs_page(data, page=1):
    return _render_runs(data["runs"], page)


def _render_tailoring(data):
    return _render_quality(data["quality"])


def _render_system_health(data):
    return ("<p class='intro'>Freshness and availability come from the latest cached snapshot. "
            "Opening this page does not refresh or contact any source.</p>"
            "<section class='panel'><h3>Source health</h3>{}</section>".format(
                _render_sources(data["sources"])))


PAGE_RENDERERS = {
    "home": _render_home,
    "applications": _render_applications_page,
    "analytics": _render_analytics,
    "runs": _render_runs_page,
    "tailoring": _render_tailoring,
    "system-health": _render_system_health,
}


def render_snapshot_html(snapshot, data=None, page="home", query_state=None,
                         application_key=None, run_key=None):
    """Render exactly one dashboard page from a cached snapshot."""
    detail_page = page == "application-detail"
    run_detail_page = page == "run-detail"
    if page not in PAGE_RENDERERS and not detail_page and not run_detail_page:
        raise ValueError("unknown dashboard page")
    if detail_page:
        content = _render_application_detail(_application_detail(snapshot, application_key))
        shell_page = "applications"
    elif page == "run-detail":
        content = _render_run_detail(_run_detail(snapshot, run_key))
        shell_page = "runs"
    else:
        data = data or build_snapshot_data(snapshot)
        if page == "applications":
            intro = ("<p class='intro'>Canonical applications from the last cached snapshot. "
                     "Unknown stages remain unmatched; missing submission dates and next actions stay unknown.</p>")
            content = intro + _render_applications(data["applications"], data["sankey"],
                                                    data["sources"], query_state)
        elif page == "runs":
            content = PAGE_RENDERERS[page](data, query_state or 1)
        else:
            content = PAGE_RENDERERS[page](data)
        shell_page = page
    metadata = (("Snapshot ID", _display(snapshot.get("snapshot_id"), "unknown", 128)),
                ("Generated at", _display(snapshot.get("generated_at"), "unknown", 48)),
                ("Schema version", _display(snapshot.get("schema_version"), "unknown", 24)))
    return dashboard_shell.render(
        shell_page, content,
        _display(snapshot.get("as_of"), "unknown", 48), metadata, DASHBOARD_CSS)


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
