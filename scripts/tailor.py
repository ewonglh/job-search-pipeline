#!/usr/bin/env python3
"""Tailoring lane: turn new postings into agent work items (stdlib only).

This is the *mechanical* half of the resume-tailoring lane. It reads the
posting store (``data/postings.json``) and the application tracker
(``applications.yaml``) from a clone of your resume repo, selects postings
that are not yet in the tracker, fetches each job description over plain HTTP,
resolves the company's own apply URL when the page exposes it, and writes one
JSON *work item* per posting under ``work/<slug>.json``.

The judgement half — selecting, rewriting and compressing resume evidence — is
an agent turn, not code. See README.md for the exact per-work-item procedure
and the tailoring doctrine repo (``JOBSEARCH_DOCTRINE_REPO``, optional).

Postings whose job description cannot be fetched are marked ``needs_jd`` and
skipped; the lane never guesses a description. LinkedIn pages are fetched over
plain HTTP first (the public job page embeds the description); if that yields
nothing usable the posting is marked ``needs_jd`` rather than driven through a
browser here.

Stdlib only (urllib, re, json, subprocess, datetime...). No secrets, no model
calls, no cron jobs created by this code.

Usage:
  tailor.py [--list] [--limit N] [--dry-run] [--json]
            [--repo DIR] [--work DIR]
"""
import argparse
import html as html_mod
import json
import os
from posting_guard import guard_text
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.environ.get("JOBSEARCH_REPO", os.path.join(HERE, "repo"))
WORK_DIR = os.environ.get("JOBSEARCH_WORK", os.path.join(HERE, "work"))
GH_REPO = os.environ.get("JOBSEARCH_GH_REPO", "example-user/resume")
DOCTRINE_REPO = os.environ.get("JOBSEARCH_DOCTRINE_REPO",
                               "example-user/resume-tailor-doctrine")
BRANCH_PREFIX = "app/"
# One authoritative all-states PR index (see pr_index). Posting links that
# already shipped in a MERGED PR are in flight just like open ones; the
# open-only guard could not see them and re-tailored the same posting.
PR_INDEX_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "pr_index_cache.json")
PR_INDEX_TTL = 900
POSTINGS_PATH = "data/postings.json"
PROFILE_PATH = "data/profile.yaml"
TRACKER_PATH = "applications.yaml"
# Owner's deliberate skips (fit / eligibility), kept outside the resume repo
# because a skip is not a submission. Entries are shaped like tracker entries
# so the same key normalisation applies; see skipped.yaml for the fields.
SKIPPED_PATH = "skipped.yaml"
SKIPPED_FILE = os.environ.get("JOBSEARCH_SKIPPED", os.path.join(HERE, SKIPPED_PATH))
# Temporary posting gates (per-company caps with a known reset window).
GATE_FILE = os.environ.get("JOBSEARCH_GATES", os.path.join(HERE, "gates.yaml"))
QUEUE_FILE = os.environ.get("JOBSEARCH_QUEUE", os.path.join(HERE, "queue.yaml"))
MASTER_PATH = "master/resume.tex"
CONTACT_URL = os.environ.get("JOBSEARCH_CONTACT_URL", "https://github.com/example-user")
USER_AGENT = "job-search-tailor/1.0 (+%s)" % CONTACT_URL
SGT = timezone(timedelta(hours=8))
FETCH_SLEEP = float(os.environ.get("JOBSEARCH_TAILOR_SLEEP", "0.5"))
MIN_JD_LEN = 200  # shorter extractions are treated as a failed fetch

# Company domains that are never the "apply" target (social / sharing / boards).
APPLY_HOST_SKIP_RE = re.compile(
    r"(internsg\.com|linkedin\.com|facebook\.com|twitter\.com|x\.com|"
    r"instagram\.com|whatsapp\.com|discord\.com|discord\.gg|reddit\.com|"
    r"line\.me|telegram\.me|t\.me|google\.com/maps|wa\.me|bit\.ly|"
    r"youtube\.com|tiktok\.com|licdn\.com|licdn\.cn|static\.)",
    re.I,
)
# Signals that an external link is the company's own application route.
APPLY_HINT_RE = re.compile(r"(career|apply|job|recruit|join|hiring|greenhouse|lever|workday|smartrecruiters|ashby)", re.I)
COVER_LETTER_RE = re.compile(r"\bcover(?:ing)?\s+letter\b", re.I)


# ---------------------------------------------------------------- yaml subset
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
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [] if not inner else [_unq(x.strip()) for x in inner.split(",")]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "~", ""):
        return None
    return _unq(v)


def parse_yaml_map(text):
    """Minimal YAML subset: a top-level mapping of scalars (profile.yaml)."""
    out = {}
    for raw in text.splitlines():
        s = _strip_comment(raw).strip()
        if not s or s in ("---", "..."):
            continue
        if ":" in s:
            k, _, v = s.partition(":")
            out[k.strip()] = _scalar(v)
    return out


def parse_yaml_list(text):
    """Minimal YAML subset: a top-level list of mappings (applications.yaml).

    Supports ``- key: value`` list items, continuation ``key: value`` lines, and
    a bare ``[]`` empty list. Nested structures are not needed by the tracker.
    """
    items, cur = [], None
    for raw in text.splitlines():
        line = _strip_comment(raw)
        s = line.strip()
        if not s or s in ("---", "...", "[]"):
            continue
        if s.startswith("- "):
            cur = {}
            items.append(cur)
            s = s[2:].strip()
            if s:
                k, _, v = s.partition(":")
                cur[k.strip()] = _scalar(v)
        elif cur is not None and ":" in s:
            k, _, v = s.partition(":")
            cur[k.strip()] = _scalar(v)
    return items


# ---------------------------------------------------------------- helpers
def slugify(text, maxlen=60):
    text = html_mod.unescape(str(text or "")).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return (text[:maxlen].rstrip("-")) or "item"


def norm_text(s):
    return re.sub(r"\s+", " ", html_mod.unescape(s or "")).strip()


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def _ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE") or None
    try:
        return ssl.create_default_context(cafile=cafile)
    except Exception:
        return ssl.create_default_context()


def fetch(url, timeout=30):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _retry_after(exc, default):
    try:
        ra = exc.headers.get("Retry-After")
    except Exception:
        return default
    try:
        return max(0.0, min(float(ra), 60.0))
    except (TypeError, ValueError):
        return default


def fetch_jd_html(url, attempts=3, log=lambda *a: None):
    """GET with bounded retries/backoff; honours Retry-After (capped at 60s)."""
    import time
    last = None
    for i in range(attempts):
        try:
            return fetch(url)
        except urllib.error.HTTPError as exc:
            last = exc
            retryable = exc.code in (429, 500, 502, 503, 504)
            wait = _retry_after(exc, 2.0 * (i + 1)) if retryable else 0
            if i < attempts - 1 and retryable:
                log(f"tailor: fetch {exc.code} {url}; retrying in {wait:.0f}s")
                time.sleep(wait)
                continue
            log(f"tailor: fetch failed {url}: HTTP {exc.code}")
            return None
        except Exception as exc:  # noqa: BLE001 - network/parse failures are expected
            last = exc
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
                continue
    log(f"tailor: fetch failed {url}: {last}")
    return None


def html_to_text(fragment):
    """Turn an HTML fragment into readable plain text, keeping list items."""
    s = fragment[0] if isinstance(fragment, tuple) else fragment
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<li\b[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"<br\b[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|h[1-6]|ul|ol|tr|dd|dt|section)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in s.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


# ---------------------------------------------------------------- JD fetch
def _internsg_jd(html_text):
    """Return (jd_text, raw_block) for an InternSG job page."""
    m = re.search(
        r'class="inter-job-details-main-panel[^"]*"[^>]*>(.*?)'
        r'<div class="inter-job-details-main-panel-bottom',
        html_text, re.S)
    if not m:
        return None
    return html_to_text(m.group(1)), m.group(1)


def _linkedin_jd(html_text):
    """Return (jd_text, raw_block) for a public LinkedIn job page."""
    for cls in ("show-more-less-html__markup", "description__text",
                "jobs-description__content"):
        m = re.search(r'class="[^"]*' + re.escape(cls) + r'[^"]*"[^>]*>(.*?)</div>',
                      html_text, re.S)
        if m:
            text = html_to_text(m.group(1))
            if text:
                return text, m.group(1)
    return None


def _generic_jd(html_text):
    m = re.search(r"<body\b[^>]*>(.*)</body>", html_text, re.S | re.I)
    body = m.group(1) if m else html_text
    return html_to_text(body), body


def _extract_apply_url(html_text, posting_link, source):
    """Best-effort: the company's own apply/careers URL, if the page exposes it."""
    base_host = urllib.parse.urlsplit(posting_link).netloc.lower()
    candidates = []
    for m in re.finditer(r'<a\b([^>]*)>(.*?)</a>', html_text, re.S | re.I):
        href = re.search(r'href\s*=\s*"([^"]+)"', m.group(1))
        if not href:
            continue
        url = html_mod.unescape(href.group(1)).strip()
        if not url.startswith(("http://", "https://")):
            continue
        host = urllib.parse.urlsplit(url).netloc.lower()
        if host == base_host or APPLY_HOST_SKIP_RE.search(host):
            continue
        text = norm_text(m.group(2))
        score = 0
        if APPLY_HINT_RE.search(url):
            score += 2
        if APPLY_HINT_RE.search(text):
            score += 2
        if score:
            candidates.append((score, url, text))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: (-c[0], len(c[1])))
    _, url, text = candidates[0]
    src = "jds-link" if text else "jds-url"
    return normalize_url(url), src


def _workday_jd(url):
    """Return (jd_text, raw_block) for a Workday job posting.

    A Workday posting page is a JS shell: the HTML carries no description, so
    the generic extractor yields nothing and the posting dead-ends at
    needs_jd (measured 2026-10-31: Razer and CapitaLand rows, 16 KB of empty
    shell). The description lives behind the same cxs API the listing adapter
    uses: ``https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>
    /<externalPath>`` -> ``jobPostingInfo.jobDescription``. Every part of that
    URL is already in the canonical posting link, which is why the board lane
    emits those links in the first place.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    m = re.match(r"^([^.]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    path_m = re.match(r"^/([^/]+)/([^/]+)(/job/.+)$", parts.path)
    if not m or not path_m:
        return None
    tenant, _wd = m.group(1), m.group(2)
    locale, site, job_path = path_m.group(1), path_m.group(2), path_m.group(3)
    api = (f"https://{host}/wday/cxs/{tenant}/{site}{job_path}")
    try:
        body = fetch(api, timeout=25)
        data = json.loads(body)
    except Exception:  # noqa: BLE001 - a bad guess must fall through, not raise
        return None
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    jd = (info or {}).get("jobDescription") or ""
    if not jd:
        return None
    text = html_to_text(jd)
    if not text:
        return None
    # The API answer is the description block itself: use it as the scope so
    # apply-link resolution cannot pick up page chrome (there is none).
    return text, jd


def _extract_jd(html_text, source, link=""):
    """Return (jd_text, scope_html) using the source-specific extractor first."""
    if source == "InternSG":
        got = _internsg_jd(html_text)
    elif source == "LinkedIn":
        got = _linkedin_jd(html_text)
    else:
        got = None
    if got and got[0] and len(got[0]) >= MIN_JD_LEN:
        return got
    if got is None and link:
        # Workday/other JS-shell posting pages: ask the ATS API instead of
        # accepting the empty shell the generic extractor would return.
        wd = _workday_jd(link)
        if wd and len(wd[0]) >= MIN_JD_LEN:
            return wd
    generic = _generic_jd(html_text)
    if generic and generic[0] and len(generic[0]) >= MIN_JD_LEN:
        return generic
    return got  # may be (short text, block) or None


def fetch_jd(posting):
    """Return dict(jd_text, apply_url, apply_url_source) or None on failure."""
    link = posting.get("link", "")
    source = posting.get("source", "")
    html_text = fetch_jd_html(link)
    if html_text is None:
        return None
    got = _extract_jd(html_text, source, link=link)
    if not got:
        return None
    jd, scope_html = got
    if not jd or len(jd) < MIN_JD_LEN:
        return None
    # Resolve the company apply URL from the description block only, so page
    # chrome (share buttons, footer, Discord invites) is not mistaken for it.
    apply_url, apply_src = _extract_apply_url(scope_html, link, source)
    return {"jd_text": jd, "apply_url": apply_url, "apply_url_source": apply_src}


# ---------------------------------------------------------------- selection
def load_repo(repo_dir):
    postings_path = os.path.join(repo_dir, POSTINGS_PATH)
    profile_path = os.path.join(repo_dir, PROFILE_PATH)
    tracker_path = os.path.join(repo_dir, TRACKER_PATH)
    with open(postings_path, encoding="utf-8") as fh:
        postings = json.load(fh)
    profile = {}
    if os.path.isfile(profile_path):
        with open(profile_path, encoding="utf-8") as fh:
            profile = parse_yaml_map(fh.read())
    apps = []
    if os.path.isfile(tracker_path):
        with open(tracker_path, encoding="utf-8") as fh:
            apps = parse_yaml_list(fh.read())
    return postings, profile, apps


def tracker_keys(apps):
    """Normalised link + company|role keys that count as already tracked."""
    links, names = set(), set()
    for a in apps:
        if not isinstance(a, dict):
            continue
        link = normalize_url(str(a.get("link") or ""))
        if link:
            links.add(link.lower())
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


def load_skipped(path):
    """Parse skipped.yaml, or [] when absent/unreadable."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            entries = parse_yaml_list(fh.read())
    except Exception:
        return []
    return [e for e in entries if isinstance(e, dict)]


def load_queue_entries(path):
    """Parse the application queue store, or [] when absent/unreadable."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            entries = parse_yaml_list(fh.read())
    except Exception:
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("pr_number")]


def is_generic_posting(posting):
    """True when the link is a search/listing page rather than a real posting.

    LinkedIn rows captured without their per-posting URL all normalise to the
    same ``/jobs/search`` link: it cannot be joined to an in-flight PR and its
    page text is search boilerplate, so tailoring it would mean inventing
    requirements. Such postings are routed to ``needs_jd`` instead.
    """

    parts = urllib.parse.urlsplit(str(posting.get("link") or ""))
    path = parts.path.lower()
    return parts.netloc.lower().endswith("linkedin.com") and (
        path.startswith("/jobs/search") or path.startswith("/jobs/browse"))


def select_postings(postings, apps, skipped=None, queue_items=None):
    """Postings that are neither already tracked nor owner-skipped.

    ``queue_items`` (queue.yaml entries) exclude postings already in the
    application pipeline: queued/in-progress items are pending submissions,
    and failed/skipped ones were already handled by the drain. Without this,
    a merged-and-queued posting re-emits as a "new" candidate every tailor
    run until its submission lands in the tracker (the apply-then-tailor
    timing leak, seen 2026-10-13..15).
    """
    links, names = tracker_keys(apps)
    if queue_items:
        q_links, q_names = tracker_keys(queue_items)
        links |= q_links
        names |= q_names
    selected = [p for p in postings if not is_tracked(p, links, names)]
    if skipped:
        s_links, s_names = tracker_keys(skipped)
        selected = [p for p in selected if not is_tracked(p, s_links, s_names)]
    return [p for p in selected if not gate_hold(p)]


def gate_hold(posting):
    """True when a company is under a temporary posting gate.

    Reads GATE_FILE (gates.yaml): entries shaped like tracker entries plus
    ``gate_until`` (ISO date). A posting whose company matches (or whose link
    host matches a listed host) is excluded from selection until that date.
    Used for per-company caps with a known reset (e.g. ByteDance/TikTok
    project application cap); delete the entry when the cap lifts.
    """
    gates = load_gates()
    if not gates:
        return False
    host = urllib.parse.urlsplit(str(posting.get("link") or "")).netloc.lower()
    for g in gates:
        until = str(g.get("gate_until") or "")
        if until and until < datetime.now().strftime("%Y-%m-%d"):
            continue
        g_host = str(g.get("host") or "").lower()
        g_names = g.get("company_aliases") or []
        if g_names:
            comp = norm_text(posting.get("company", "")).lower()
            if any(norm_text(a).lower() == comp for a in g_names):
                return True
        if g_host and (host == g_host or host.endswith("." + g_host)):
            return True
    return False


def load_gates(path=None):
    """Parse gates.yaml, or [] when absent/unreadable."""
    path = path or GATE_FILE
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return [e for e in parse_yaml_list(fh.read()) if isinstance(e, dict)]
    except Exception:
        return []


# ---------------------------------------------------------------- in-flight
# "In flight" means the posting already has an **open PR** in the resume repo.
# A bare ``app/*`` branch with no open PR is deliberately NOT in flight: it must
# be completed (its PR opened) rather than skipped, or it would be stuck
# forever. The tracker entry ships inside each PR, so ``applications.yaml`` on
# ``main`` is empty until merge; we therefore read the tracker entry from each
# open-PR head branch (one posting per branch) and match postings by link or
# company|role. Matching on the branch *content* rather than the branch *name*
# avoids skipping a brand-new role at a company that already has a PR.
def company_slug(posting):
    return slugify(posting.get("company", ""))


def branch_matches_company(branch, slug):
    """True for ``app/<slug>`` or ``app/<slug>-<descriptor>``.

    The trailing ``-`` delimiter keeps ``app/sea`` from matching
    ``app/seattle-x`` while still catching role-suffixed branches such as
    ``app/one-tech-stop-pte-ltd-data-scientist-2``. Used only as a fallback for
    a branch whose tracker entry could not be read.
    """
    base = BRANCH_PREFIX + slug
    return bool(slug) and (branch == base or branch.startswith(base + "-"))


def is_app_branch(branch):
    """True for any ``app/*`` branch (parity with ``queue.is_app_branch``)."""
    return str(branch or "").startswith(BRANCH_PREFIX)


def _git(repo, *args, timeout=30):
    return subprocess.run(
        ["git", "-C", repo, "-c", "credential.helper=!gh auth git-credential", *args],
        capture_output=True, text=True, timeout=timeout)


def remote_app_branches(repo_dir, log=lambda *a: None):
    """Remote ``app/*`` branch names, or ``None`` when the check is unavailable."""
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        log(f"tailor: no clone at {repo_dir}; skipping branch check")
        return None
    try:
        proc = _git(repo_dir, "ls-remote", "--heads", "origin", "refs/heads/app/*")
    except Exception as exc:  # noqa: BLE001 - network/subprocess failures are expected
        log(f"tailor: branch check failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"tailor: branch check failed: {proc.stderr.strip() or proc.returncode}")
        return None
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.add(parts[1][len("refs/heads/"):])
    return names


def open_pr_heads(log=lambda *a: None):
    """Head branch names of open PRs, or ``None`` when the check is unavailable."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", GH_REPO, "--state", "open",
             "--json", "headRefName,url", "--limit", "1000"],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        log(f"tailor: PR check failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"tailor: PR check failed: {proc.stderr.strip() or proc.returncode}")
        return None
    try:
        return {p.get("headRefName", "") for p in json.loads(proc.stdout or "[]")}
    except ValueError:
        log("tailor: PR check returned invalid JSON")
        return None


def _body_links(body):
    """Normalised non-github posting URLs in a PR body (lowercased).

    Collect *every* posting URL, not just the first: a repost that a PR body
    lists as an alias ("same role re-listed at <url>") must count as in flight,
    otherwise each repost resurfaces as new work every run.
    """
    links = set()
    for m in re.finditer(r"https?://[^\s)\]<>{\"']+", str(body or "")):
        url = m.group(0)
        if "github.com" not in url:
            links.add(normalize_url(url).lower())
    return links


def _read_pr_cache():
    try:
        with open(PR_INDEX_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def _write_pr_cache(fetched_at, items):
    """Atomic cache write: a temp file in the same dir, then ``os.replace``."""
    tmp = PR_INDEX_CACHE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": fetched_at, "items": items}, fh,
                      ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, PR_INDEX_CACHE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _pr_index_from(items, stale, fetched_at):
    """Shape the guard view from raw PR rows.

    ``open_heads`` / ``open_links`` keep the old open-only semantics;
    ``merged_links`` and ``closed_links`` map a normalised posting link to the
    number of the PR that already carried it, which is what makes a shipped
    posting count as in flight.
    """
    open_heads, open_links, merged, closed = set(), set(), {}, {}
    for p in items:
        if not isinstance(p, dict) or not is_app_branch(p.get("headRefName", "")):
            continue
        state = str(p.get("state") or "").upper()
        number = int(p.get("number") or 0)
        if state == "OPEN":
            open_heads.add(p.get("headRefName", ""))
        bucket = None
        if state == "MERGED":
            bucket = merged
        elif state == "CLOSED":
            bucket = closed
        for link in _body_links(p.get("body")):
            if state == "OPEN":
                open_links.add(link)
            if bucket is not None:
                bucket.setdefault(link, number)
    return {"open_heads": open_heads, "open_links": open_links,
            "merged_links": merged, "closed_links": closed,
            "stale": bool(stale), "fetched_at": fetched_at}


def pr_index(log=lambda *a: None, refresh=False):
    """One cached, authoritative view of the repo's PRs in every state.

    Returns a dict with ``open_heads``, ``open_links``, ``merged_links``,
    ``closed_links``, ``stale`` and ``fetched_at``, or ``None`` when GitHub
    cannot be reached and no cache exists (callers then warn and proceed, as
    before). Read-through cache at ``PR_INDEX_CACHE`` with ``PR_INDEX_TTL``;
    ``refresh=True`` bypasses it. On failure the cached value is returned with
    ``stale=True`` so a transient gh outage cannot resurrect shipped postings.
    """
    cached = None if refresh else _read_pr_cache()
    if cached is not None:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                str(cached.get("fetched_at")).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = PR_INDEX_TTL + 1
        if age < PR_INDEX_TTL:
            return _pr_index_from(cached["items"], False, cached.get("fetched_at"))
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", GH_REPO, "--state", "all",
             "--json", "number,state,headRefName,body,mergedAt,url",
             "--limit", "1000"],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"gh exit {proc.returncode}")
        items = json.loads(proc.stdout or "[]")
        if not isinstance(items, list):
            raise RuntimeError("gh returned non-list JSON")
    except Exception as exc:  # noqa: BLE001 - gh/network failures are expected
        cached = _read_pr_cache()
        if cached is None:
            log(f"tailor: PR check failed: {exc}")
            return None
        log(f"tailor: WARNING PR index unavailable ({exc}); using cached index "
            f"from {cached.get('fetched_at')}")
        return _pr_index_from(cached["items"], True, cached.get("fetched_at"))
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_pr_cache(fetched_at, items)
    return _pr_index_from(items, False, fetched_at)


def open_pr_body_links(log=lambda *a: None):
    """Normalised posting links from open-PR bodies, or ``None`` when down.

    Since 2026-10-08 tailor PRs carry the posting link in the body (first bare
    URL, per TAILOR_BRIEF.md) and never edit ``applications.yaml`` — so the
    body is the primary in-flight key; branch trackers remain a legacy
    fallback.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", GH_REPO, "--state", "open",
             "--json", "headRefName,body", "--limit", "1000"],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        log(f"tailor: PR check failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"tailor: PR check failed: {proc.stderr.strip() or proc.returncode}")
        return None
    try:
        prs = json.loads(proc.stdout or "[]")
    except ValueError:
        log("tailor: PR check returned invalid JSON")
        return None
    links = set()
    for p in prs:
        if not isinstance(p, dict) or not is_app_branch(p.get("headRefName", "")):
            continue
        links |= _body_links(p.get("body"))
    return links


def ensure_app_refs(repo_dir, log=lambda *a: None):
    """Fetch remote ``app/*`` branches into ``refs/remotes/origin/app/*``.

    Best-effort: existing refs are still usable if the fetch fails.
    """
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return False
    try:
        proc = _git(repo_dir, "fetch", "--quiet", "--no-tags", "origin",
                    f"+refs/heads/{BRANCH_PREFIX}*:refs/remotes/origin/{BRANCH_PREFIX}*")
    except Exception as exc:  # noqa: BLE001
        log(f"tailor: fetch of app/* failed: {exc}")
        return False
    if proc.returncode != 0:
        log(f"tailor: fetch of app/* failed: {proc.stderr.strip() or proc.returncode}")
        return False
    return True


def branch_tracker_entries(repo_dir, branch, log=lambda *a: None):
    """Parsed ``applications.yaml`` entries on a remote-tracking branch, or None."""
    proc = _git(repo_dir, "show", f"origin/{branch}:{TRACKER_PATH}")
    if proc.returncode != 0:
        return None
    try:
        return parse_yaml_list(proc.stdout)
    except Exception:  # noqa: BLE001
        return None


def in_flight_tracker(repo_dir, open_heads, log=lambda *a: None):
    """Collect tracker keys from open-PR head branches.

    Returns ``(links, names, unreadable)`` where ``unreadable`` lists branches
    whose tracker entry could not be read (matched conservatively by company).
    """
    links, names, unreadable = set(), set(), []
    for b in sorted(open_heads):
        entries = branch_tracker_entries(repo_dir, b, log)
        if entries is None:
            unreadable.append(b)
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            link = normalize_url(str(e.get("link") or "")).lower()
            if link:
                links.add(link)
            c = norm_text(e.get("company", "")).lower()
            r = norm_text(e.get("role", "")).lower()
            if c or r:
                names.add(f"{c}|{r}")
    return links, names, unreadable


def posting_in_flight(posting, links, names, unreadable):
    """True when the posting already has an open PR."""
    link = normalize_url(posting.get("link", "")).lower()
    if link and link in links:
        return True
    company = norm_text(posting.get("company", "")).lower()
    role = norm_text(posting.get("role", "")).lower()
    if f"{company}|{role}" in names:
        return True
    return any(branch_matches_company(b, company_slug(posting)) for b in unreadable)


def partition_inflight(selected, links, names, unreadable=()):
    """Split ``[(posting, slug)]`` into (ready, skipped) by open PRs."""
    ready, skipped = [], []
    for posting, slug in selected:
        target = skipped if posting_in_flight(posting, links, names, unreadable) else ready
        target.append((posting, slug))
    return ready, skipped


def bare_app_branches(repo_dir, open_heads, log=lambda *a: None):
    """``app/*`` branches with no open PR: they need a PR, not a skip."""
    branches = remote_app_branches(repo_dir, log)
    if branches is None:
        return None
    return sorted(b for b in branches if b not in open_heads)


def assign_slugs(selected):
    """Deterministic, collision-free slugs for work-item filenames."""
    used, out = {}, []
    for p in selected:
        base = f"{slugify(p.get('company', ''))}-{slugify(p.get('role', ''))}"
        n = used.get(base, 0) + 1
        used[base] = n
        slug = base if n == 1 else f"{base}-{n}"
        out.append((p, slug))
    return out


# ---------------------------------------------------------------- work items
def build_work_item(posting, slug, jd, profile):
    return {
        "slug": slug,
        "company": posting.get("company", ""),
        "role": posting.get("role", ""),
        "link": posting.get("link", ""),
        "source": posting.get("source", ""),
        "location": posting.get("location", ""),
        "posted": posting.get("posted", ""),
        "status": "ready",
        "jd_text": jd["jd_text"],
        "apply_url": jd.get("apply_url"),
        "apply_url_source": jd.get("apply_url_source"),
        "cover_letter_requested": bool(COVER_LETTER_RE.search(jd["jd_text"])),
        "profile": profile,
        "doctrine_repo": DOCTRINE_REPO,
        "master": MASTER_PATH,
        "generated_at": datetime.now(SGT).isoformat(timespec="seconds"),
    }


def write_work_item(work_dir, slug, item):
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, slug + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(item, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def write_needs_jd(work_dir, pending):
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, "_needs_jd.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": datetime.now(SGT).isoformat(timespec="seconds"),
                   "count": len(pending), "postings": pending}, fh, indent=2,
                  ensure_ascii=False)
        fh.write("\n")
    return path


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="list candidate postings (not yet tracked); no fetch, no writes")
    ap.add_argument("--limit", type=int, default=0,
                    help="at most N work items (0 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + resolve everything but write no work-item files")
    ap.add_argument("--force", action="store_true",
                    help="ignore existing app/* branches and open PRs (deliberate re-tailor)")
    ap.add_argument("--json", action="store_true",
                    help="emit work items / candidates as JSON on stdout")
    ap.add_argument("--repo", default=REPO_DIR, help="resume clone path")
    ap.add_argument("--work", default=WORK_DIR, help="work-item output directory")
    ap.add_argument("--skipped", default=SKIPPED_FILE,
                    help="owner-skip list to exclude from selection")
    ap.add_argument("--queue", default=QUEUE_FILE,
                    help="application queue store; its postings are excluded "
                         "from selection (empty/missing file = no-op)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)

    postings, profile, apps = load_repo(args.repo)
    skipped_entries = load_skipped(args.skipped)
    queue_entries = load_queue_entries(args.queue)
    selected = assign_slugs(
        select_postings(postings, apps, skipped_entries, queue_entries))

    # Skip postings that already have an OPEN PR. The tracker entry ships inside
    # each PR, so applications.yaml on main stays empty until merge; reading the
    # open-PR head branches is what keeps repeated runs idempotent. A bare app/*
    # branch (no open PR) is completed, not skipped.
    skipped_inflight, bare = [], []
    if args.force:
        log("tailor: --force set; ignoring open PRs")
    else:
        # One all-states index: an OPEN PR means the work is in flight, and a
        # MERGED/CLOSED one carrying the same posting link means this posting
        # already shipped (the queue or the tracker may still be catching up).
        # Open-only listing missed the second case and re-tailored the posting.
        index = pr_index(log)
        if index is None:
            log("tailor: WARNING in-flight check unavailable; open PRs could not be detected")
        else:
            open_heads = index["open_heads"]
            ensure_app_refs(args.repo, log)
            links, names, unreadable = in_flight_tracker(args.repo, open_heads, log)
            # Primary key since 2026-10-08: the PR body's posting link (the
            # branches carry variants only). Legacy branch trackers and the
            # company-name fallback for unreadable branches still apply.
            links |= index["open_links"]
            dup = sorted(set(index["merged_links"]) & set(index["closed_links"]))
            settled = dict(index["merged_links"])
            for link, number in index["closed_links"].items():
                settled.setdefault(link, number)
            before = len(links)
            links |= set(settled)
            if len(links) > before:
                log(f"tailor: NOTE {len(links) - before} posting link(s) already "
                    f"carried by a merged/closed PR count as in flight")
            if dup:
                log(f"tailor: NOTE {len(dup)} link(s) appear in both a merged and a "
                    "closed PR")
            if unreadable:
                log(f"tailor: NOTE could not read the tracker entry for "
                    f"{len(unreadable)} open-PR branch(es); matching those by company name")
            selected, skipped_inflight = partition_inflight(selected, links, names, unreadable)
            if skipped_inflight:
                log(f"tailor: skipped {len(skipped_inflight)} posting(s) already in flight; "
                    f"use --force to re-tailor")
            bare = bare_app_branches(args.repo, open_heads, log) or []
            if bare:
                log(f"tailor: NOTE {len(bare)} app/* branch(es) have no open PR and will be "
                    f"completed (PR opened) rather than skipped: {', '.join(bare)}")

    generic = [(p, s) for (p, s) in selected if is_generic_posting(p)]
    if generic:
        log(f"tailor: NOTE {len(generic)} posting(s) carry a generic search/listing "
            "URL with no per-posting page; routing them to needs_jd")
        selected = [(p, s) for (p, s) in selected if not is_generic_posting(p)]

    if args.limit and args.limit > 0:
        selected = selected[:args.limit]

    if args.list:
        if args.json:
            print(json.dumps([{"company": p.get("company"), "role": p.get("role"),
                               "link": p.get("link"), "slug": s}
                              for p, s in selected], indent=2, ensure_ascii=False))
        else:
            for p, s in selected:
                print(f"{p.get('company')} · {p.get('role')} · {p.get('link')}")
        log(f"tailor: {len(postings)} posting(s) in store, {len(apps)} tracked, "
            f"{len(skipped_entries)} owner-skipped, {len(queue_entries)} in queue, "
            f"{len(skipped_inflight)} with an open PR, {len(bare)} bare branch(es), "
            f"{len(generic)} generic-link, {len(selected)} candidate(s)")
        return 0

    created, pending, items = [], [], []
    pending.extend({"company": p.get("company"), "role": p.get("role"),
                    "link": p.get("link"), "slug": slug,
                    "reason": "generic-link"}
                   for p, slug in generic)
    for i, (p, slug) in enumerate(selected):
        jd = fetch_jd(p)
        if jd is None:
            pending.append({"company": p.get("company"), "role": p.get("role"),
                            "link": p.get("link"), "slug": slug,
                            "reason": "jd-unavailable"})
            log(f"tailor: needs_jd  {p.get('company')} · {p.get('role')}")
            continue
        # Posting-integrity gate: JD text is stranger-written and flows into
        # agent contexts, so the work item carries only the sanitized text
        # plus its guard summary. Hostile postings are never tailored.
        guard = guard_text(jd["jd_text"])
        jd["jd_text"] = guard["clean_text"]
        jd_guard = {"verdict": guard["verdict"],
                    "n_findings": len(guard["findings"]),
                    "kinds": sorted({f["kind"] for f in guard["findings"]})}
        log(f"tailor: guard {guard['verdict']} {slug} "
            f"({len(guard['findings'])} findings)")
        if guard["verdict"] == "hostile":
            pending.append({"company": p.get("company"), "role": p.get("role"),
                            "link": p.get("link"), "slug": slug,
                            "reason": "guard_hostile",
                            "verdict": guard["verdict"],
                            "findings": guard["findings"]})
            log(f"tailor: needs_jd  {p.get('company')} · {p.get('role')} "
                "(guard_hostile)")
            continue
        item = build_work_item(p, slug, jd, profile)
        item["jd_guard"] = jd_guard
        items.append(item)
        if not args.dry_run:
            created.append(write_work_item(args.work, slug, item))
        log(f"tailor: work      {slug}  ({len(jd['jd_text'])} chars"
            f"{', apply=' + jd['apply_url'] if jd.get('apply_url') else ''})")
        if i < len(selected) - 1 and FETCH_SLEEP:
            import time
            time.sleep(FETCH_SLEEP)

    if not args.dry_run and pending:
        write_needs_jd(args.work, pending)

    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        for item in items:
            print(f"{item['company']} · {item['role']} · {item['link']} · "
                  f"work/{item['slug']}.json")
    log(f"tailor: {'would create' if args.dry_run else 'created'} {len(items)} work item(s), "
        f"{len(pending)} needs_jd, {len(skipped_inflight)} already with an open PR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
