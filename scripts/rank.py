#!/usr/bin/env python3
"""Fit-ranking lane: deterministic, zero-token ranked shortlist of postings.

This is the *mechanical* half of the fit-ranking lane. It selects candidate
postings (not tracked, not owner-skipped, no open tailor PR), fetches each job
description over plain HTTP (cached in ``rank_cache.json``), vetoes and scores
them against ``rubric.yaml``, and prints a ranked digest on stdout plus full
detail in ``rankings.json``.

A future agent judgement layer may re-rank the top-N; this lane never calls a
model and needs no secrets. Stdlib only, no cron jobs created by this code.

Usage:
  rank.py [--dry-run] [--json] [--limit N] [--top N]
          [--repo DIR] [--skipped PATH] [--cache FILE] [--prs FILE]
          [--rankings FILE]
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import tailor as t
from posting_guard import guard_text

HERE = os.path.dirname(os.path.abspath(__file__))
SGT = timezone(timedelta(hours=8))
CONTACT_URL = os.environ.get("JOBSEARCH_CONTACT_URL", "https://github.com/example-user")
USER_AGENT = "job-search-rank/1.0 (+%s)" % CONTACT_URL
CACHE_TTL_DAYS = 30
RUBRIC_PATH = os.path.join(HERE, "rubric.yaml")
DEFAULT_CACHE = os.path.join(HERE, "rank_cache.json")
DEFAULT_RANKINGS = os.path.join(HERE, "rankings.json")

VETO_ORDER = ["requires_graduation", "clearance_citizenship", "seniority",
              "immediate_fulltime"]


# ---------------------------------------------------------------- rubric
def load_rubric(path):
    """Parse rubric.yaml with tailor's flat-mapping subset.

    Veto pairs (``veto_<name>`` / ``veto_<name>_reason``) become an ordered
    veto list; ``weight_*`` keys become dimension weights.
    """
    with open(path, encoding="utf-8") as fh:
        raw = t.parse_yaml_map(fh.read())

    def num(key):
        try:
            return float(raw.get(key, 0))
        except (TypeError, ValueError):
            return 0.0

    def words(key):
        v = raw.get(key, [])
        if isinstance(v, list):
            return [str(x).lower() for x in v]
        return [str(v).lower()] if v else []

    rubric = {
        "weights": {
            "skill_overlap": num("weight_skill_overlap"),
            "grad_year": num("weight_grad_year"),
            "availability": num("weight_availability"),
            "location": num("weight_location"),
            "freshness": num("weight_freshness"),
            "source": num("weight_source"),
        },
        "jd_skills": words("jd_skills"),
        "profile_skills": words("profile_skills"),
        "gradyear_flags": words("gradyear_flags"),
        "window_good": words("window_good"),
        "window_half": words("window_half"),
        "location_bad": words("location_bad"),
        "source_good": words("source_good"),
        "source_half": words("source_half"),
        "vetoes": [],
    }
    for name in VETO_ORDER:
        pat = raw.get(f"veto_{name}")
        if pat:
            rubric["vetoes"].append(
                (name, pat, str(raw.get(f"veto_{name}_reason", name))))
    return rubric


# ---------------------------------------------------------------- cache
def load_cache(path):
    """Read the JD cache. Missing file = {}. Corrupt = hard error, never reset."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        print(f"rank: FATAL corrupt cache {path}: {exc}; refusing to reset",
              file=sys.stderr)
        raise SystemExit(1)
    return data if isinstance(data, dict) else {}


def save_cache(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def cache_fresh(entry):
    try:
        ts = datetime.fromisoformat(str(entry.get("fetched_at", "")))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=SGT)
    return (datetime.now(SGT) - ts) < timedelta(days=CACHE_TTL_DAYS)


# ---------------------------------------------------------------- selection
def load_open_pr_links(prs_file, log):
    """Normalised posting links from open tailor PR bodies.

    ``prs_file`` is an offline fixture (gh ``pr list --json headRefName,body``
    shape); the default queries gh exactly like tailor.py.
    """
    if prs_file:
        with open(prs_file, encoding="utf-8") as fh:
            prs = json.load(fh)
        links = set()
        for p in prs:
            if not isinstance(p, dict) or not t.is_app_branch(p.get("headRefName", "")):
                continue
            m = re.search(r"https?://[^\s)\]<>\"']+", str(p.get("body") or ""))
            if m and "github.com" not in m.group(0):
                links.add(t.normalize_url(m.group(0)).lower())
        return links
    links = t.open_pr_body_links(log)
    if links is None:
        log("rank: WARNING in-flight check unavailable; open PRs could not be detected")
        return set()
    return links


def select_candidates(postings, apps, skipped, pr_links):
    """Postings that are untracked, unskipped and have no open tailor PR."""
    links, names = t.tracker_keys(apps)
    if pr_links:
        links |= {l.lower() for l in pr_links}
    cand = [p for p in postings if not t.is_tracked(p, links, names)]
    if skipped:
        s_links, s_names = t.tracker_keys(skipped)
        cand = [p for p in cand if not t.is_tracked(p, s_links, s_names)]
    return [p for p in cand if not t.gate_hold(p)]


def added_date(posting):
    try:
        return datetime.strptime(str(posting.get("added", "")), "%Y-%m-%d").date()
    except ValueError:
        return None


def sort_newest(postings):
    return sorted(postings,
                  key=lambda p: (str(p.get("added", "")), str(p.get("link", ""))),
                  reverse=True)


# ---------------------------------------------------------------- JD fetch
def fetch_jd_cached(posting, cache, log):
    """Return (jd_text, from_cache, reason, guard). ``reason`` set when unusable.

    ``jd_text`` is always the GUARDED (sanitized) text: the integrity gate
    runs before veto regexes and scoring, so an injection line can never
    trigger or suppress a veto. The cache stores the sanitized text plus a
    ``guard`` verdict key. Entries predating the gate (no ``guard`` key)
    are sanitized on read and upgraded in memory — legacy caches stay
    readable, no reset, no schema break. ``guard`` is
    {verdict, n_findings}, or None when the JD was unusable.
    """
    link = posting.get("link", "")
    key = t.normalize_url(link)
    entry = cache.get(key)
    if isinstance(entry, dict) and entry.get("jd_text") and cache_fresh(entry):
        g = guard_text(entry["jd_text"])
        if g["clean_text"] != entry["jd_text"] or "guard" not in entry:
            entry["jd_text"] = g["clean_text"]
            entry["guard"] = g["verdict"]
        return (entry["jd_text"], True, None,
                {"verdict": g["verdict"], "n_findings": len(g["findings"])})
    if t.is_generic_posting(posting):
        return None, False, "generic-link", None
    html_text = t.fetch_jd_html(link, log=log)
    if html_text is None:
        return None, False, "jd-unavailable", None
    got = t._extract_jd(html_text, posting.get("source", ""),
                        link=posting.get("link", ""))
    jd = got[0] if got else None
    if not jd or len(jd) < t.MIN_JD_LEN:
        return None, False, "jd-unavailable", None
    g = guard_text(jd)
    cache[key] = {"jd_text": g["clean_text"], "guard": g["verdict"],
                  "fetched_at": datetime.now(SGT).isoformat(),
                  "company": posting.get("company", ""),
                  "role": posting.get("role", "")}
    return (g["clean_text"], False, None,
            {"verdict": g["verdict"], "n_findings": len(g["findings"])})


# ---------------------------------------------------------------- scoring
def kw_hit(text, kw):
    return re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text) is not None


def dim_skill(jd_low, rubric):
    found = [k for k in rubric["jd_skills"] if kw_hit(jd_low, k)]
    if not found:
        return 0.5, ["no recognised skill keywords in JD"]
    matched = [k for k in found if k in rubric["profile_skills"]]
    score = len(matched) / len(found)
    if matched:
        shown = "+".join(matched[:4])
        extra = f"+{len(matched) - 4} more" if len(matched) > 4 else ""
        return score, [f"skill {shown}{extra} ({len(matched)}/{len(found)} JD keywords)"]
    return 0.0, [f"no skill overlap (JD asks {', '.join(found[:4])})"]


def dim_gradyear(jd_low, rubric):
    hits = [f for f in rubric["gradyear_flags"] if f in jd_low]
    if hits:
        return 0.4, [f"year-of-study bar ({hits[0]})"]
    return 1.0, ["open to current undergrads"]


def dim_availability(jd_low, rubric):
    if any(w in jd_low for w in rubric["window_good"]):
        return 1.0, ["window fits May-Aug/Jun-Dec 2027"]
    if any(w in jd_low for w in rubric["window_half"]):
        return 0.5, ["H1/6-month window vs May-Aug preference"]
    return 0.8, ["no window stated — assumed compatible"]


def dim_location(text_low, posting, rubric):
    if any(w in text_low for w in rubric["location_bad"]):
        return 0.3, ["JD suggests non-SG base"]
    loc = t.norm_text(posting.get("location", "")).lower()
    if "remote" in text_low or "remote" in loc or "work from home" in text_low:
        return 1.0, ["remote-friendly"]
    if "singapore" in loc or loc in ("sg", "") or posting.get("source") == "InternSG":
        return 1.0, ["singapore-based"]
    return 0.8, ["location plausible"]


def dim_freshness(posting):
    d = added_date(posting)
    if d is None:
        return 0.25, ["no added date"]
    age = (datetime.now(SGT).date() - d).days
    if age <= 7:
        return 1.0, [f"posted {age}d ago"]
    if age <= 14:
        return 0.75, [f"posted {age}d ago"]
    if age <= 30:
        return 0.5, [f"posted {age}d ago"]
    return 0.25, [f"stale ({age}d old)"]


def dim_source(posting, rubric):
    src = t.norm_text(posting.get("source", "")).lower()
    if not src:
        return 0.5, ["source unknown"]
    if src in rubric["source_good"]:
        return 1.0, ["direct SG board"]
    if src in rubric["source_half"]:
        return 0.7, ["aggregator listing"]
    return 0.9, [f"via {posting.get('source')}"]


def check_vetoes(text, rubric):
    for name, pattern, reason in rubric["vetoes"]:
        try:
            if re.search(pattern, text, re.I):
                return reason
        except re.error:
            continue
    return None


def score_posting(posting, jd_text, rubric):
    """Return (score, reasons, dimensions) or (None, veto_reason, {}).

    The JD text is re-gated here (idempotent) so direct callers get the same
    guarantee as the fetch path: veto regexes only ever see sanitized text.
    """
    jd_text = guard_text(jd_text or "")["clean_text"]
    blob = f"{posting.get('role', '')}\n{jd_text}"
    veto = check_vetoes(blob, rubric)
    if veto:
        return None, veto, {}
    jd_low = jd_text.lower()
    loc_text_low = f"{posting.get('role', '')} {jd_text}".lower()
    dims = {
        "skill_overlap": dim_skill(jd_low, rubric),
        "grad_year": dim_gradyear(jd_low, rubric),
        "availability": dim_availability(jd_low, rubric),
        "location": dim_location(loc_text_low, posting, rubric),
        "freshness": dim_freshness(posting),
        "source": dim_source(posting, rubric),
    }
    weights = rubric["weights"]
    total_w = sum(weights.values()) or 1.0
    score = sum(dims[k][0] * weights.get(k, 0) for k in dims) / total_w * 100
    ranked = sorted(dims.items(),
                    key=lambda kv: kv[1][0] * weights.get(kv[0], 0), reverse=True)
    reasons = [r for _, (_, rs) in ranked for r in rs[:1]][:3]
    return round(score), reasons, {k: v[0] for k, v in dims.items()}


def format_line(entry):
    top = entry["reasons"][0] if entry.get("reasons") else "scored"
    return f"{entry['score']} · {entry.get('company', '')} · {entry.get('role', '')} · {top}"


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + score + print, but write NOTHING")
    ap.add_argument("--json", action="store_true",
                    help="emit ranked entries as JSON on stdout")
    ap.add_argument("--limit", type=int, default=40,
                    help="max JD fetches per run (default 40, newest first)")
    ap.add_argument("--top", type=int, default=15,
                    help="digest cap: at most N lines on stdout (default 15)")
    ap.add_argument("--repo", default=t.REPO_DIR, help="resume clone path")
    ap.add_argument("--skipped", default=t.SKIPPED_FILE, help="owner-skip list")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="JD cache file")
    ap.add_argument("--prs", default=None,
                    help="offline fixture of open tailor PRs (gh JSON shape); "
                         "default queries gh like tailor.py")
    ap.add_argument("--rankings", default=DEFAULT_RANKINGS,
                    help="rankings output file")
    ap.add_argument("--rubric", default=RUBRIC_PATH, help="rubric file")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)

    rubric = load_rubric(args.rubric)
    postings, _, apps = t.load_repo(args.repo)
    skipped = t.load_skipped(args.skipped)
    pr_links = load_open_pr_links(args.prs, log)

    candidates = sort_newest(select_candidates(postings, apps, skipped, pr_links))

    cache = load_cache(args.cache)
    ranked, excluded, needs_jd, fetched = [], [], [], 0
    generic_posts = [p for p in candidates if t.is_generic_posting(p)]
    if generic_posts:
        log(f"rank: NOTE skipped {len(generic_posts)} generic search/listing URL(s)")
        for p in generic_posts:
            needs_jd.append({"company": p.get("company"), "role": p.get("role"),
                             "link": p.get("link"), "reason": "generic-link"})
        candidates = [p for p in candidates if not t.is_generic_posting(p)]
    for p in candidates:
        if args.limit and fetched >= args.limit:
            log(f"rank: fetch limit {args.limit} reached; "
                f"{len(candidates) - fetched} candidate(s) deferred")
            break
        jd, from_cache, reason, guard = fetch_jd_cached(p, cache, log)
        if jd is None:
            needs_jd.append({"company": p.get("company"), "role": p.get("role"),
                             "link": p.get("link"), "reason": reason})
            log(f"rank: needs_jd  {p.get('company')} · {p.get('role')} ({reason})")
            continue
        fetched += 0 if from_cache else 1
        score, info, dims = score_posting(p, jd, rubric)
        base = {"company": p.get("company"), "role": p.get("role"),
                "link": p.get("link"), "source": p.get("source", ""),
                "location": p.get("location", ""), "added": p.get("added", ""),
                "verdict": guard["verdict"], "n_findings": guard["n_findings"]}
        if score is None:
            excluded.append({**base, "reason": info})
            log(f"rank: veto  {p.get('company')} · {p.get('role')} ({info})")
            continue
        ranked.append({**base, "score": score, "reasons": info,
                       "dimensions": dims})
        src = "cache" if from_cache else "fetch"
        log(f"rank: scored {score:3d} ({src})  {p.get('company')} · {p.get('role')}")

    ranked.sort(key=lambda e: (-e["score"], str(e.get("company", "")),
                               str(e.get("role", ""))))
    digest = ranked[:args.top] if args.top else ranked

    if not args.dry_run:
        save_cache(args.cache, cache)
        payload = {"generated_at": datetime.now(SGT).isoformat(timespec="seconds"),
                   "count": len(ranked), "ranked": ranked,
                   "excluded": excluded, "needs_jd": needs_jd}
        tmp = args.rankings + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, args.rankings)
    else:
        log(f"rank: --dry-run: wrote nothing "
            f"({len(ranked)} ranked, {len(excluded)} vetoed, {len(needs_jd)} needs_jd)")

    if args.json:
        print(json.dumps(digest, indent=2, ensure_ascii=False))
    else:
        for entry in digest:
            print(format_line(entry))
    log(f"rank: {len(postings)} posting(s), {len(apps)} tracked, "
        f"{len(skipped)} owner-skipped, {len(pr_links)} open-PR link(s), "
        f"{len(ranked)} ranked, {len(excluded)} vetoed, {len(needs_jd)} needs_jd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
