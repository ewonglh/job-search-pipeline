#!/usr/bin/env python3
"""Headless job-posting discovery lane (stdlib only).

Fetches internship postings from InternSG (and optionally configured company
career pages), keeps the ones that look like Singapore SWE/tech internships,
and merges the *new* ones into ``data/postings.json`` of a clone of your
resume repo (pull, merge, commit, push to main). Existing entries are
never deleted or rewritten; duplicates are skipped by a normalised
``company|role|url`` key.

It never drives a browser: the LinkedIn lane feeds it via ``--import-json``.

Output is exactly one digest line per NEW posting on stdout:
    <company> · <role> · <link>
Everything else (diagnostics, git) goes to stderr.

Usage:
  discover.py [--dry-run] [--limit N] [--json] [--boards]
              [--import-json <file|->] [--companies FILE] [--repo DIR]

``--boards`` adds the ATS board sweep (``probe_boards.py``) to the fetch lane;
those rows share this lane's role filter, location filter and store dedupe.
"""
import argparse
import html as html_mod
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

SGT_OFFSET_DAYS = 0  # dates are plain local dates; kept explicit for clarity
HERE = os.path.dirname(os.path.abspath(__file__))
GH_REPO = os.environ.get("JOBSEARCH_GH_REPO", "example-user/resume")
REPO_URL = os.environ.get("JOBSEARCH_REPO_URL",
                          "https://github.com/%s.git" % GH_REPO)
REPO_DIR = os.environ.get("JOBSEARCH_REPO", os.path.join(HERE, "repo"))
COMPANIES_FILE = os.path.join(HERE, "companies.yaml")
STORE_PATH = "data/postings.json"
CONTACT_URL = os.environ.get("JOBSEARCH_CONTACT_URL", "https://github.com/example-user")
USER_AGENT = "job-search-discover/1.0 (+%s)" % CONTACT_URL
MAX_PAGES = int(os.environ.get("JOBSEARCH_PAGES", "2"))
PAGE_SLEEP = float(os.environ.get("JOBSEARCH_PAGE_SLEEP", "1.0"))

# InternSG sources: the home page plus the software/tech filtered listings.
# f_p=107 -> profession "IT / Information Technology"
# f_i=61  -> industry  "Computer and IT"
# f_i=420 -> industry  "Artificial Intelligence / Smart Automation"
INTERNSG_SOURCES = [
    ("InternSG", "https://www.internsg.com/"),
    ("InternSG", "https://www.internsg.com/jobs/?f_p=107"),
    ("InternSG", "https://www.internsg.com/jobs/?f_i=61"),
    ("InternSG", "https://www.internsg.com/jobs/?f_i=420"),
]

# ---------------------------------------------------------------- filters
# SWE-only role filter. A posting is kept when the role has an intern token
# AND an engineering token, and NO non-engineering token (exclusion wins).
#
# Engineering tokens (include):
ENG_RE = re.compile(
    r"\b(?:software|swe|developer|engineer(?:ing)?|programmer|"
    r"full[\s-]?stack|fullstack|front[\s-]?end|back[\s-]?end|mobile|android|ios|"
    r"data\s+(?:engineer|scientist|analyst)|machine\s+learning|ml|ai|"
    r"computer\s+vision|devops|cloud|platform|security\s+engineering|"
    r"qa\s+automation)\b",
    re.I,
)
# Non-engineering tokens (exclude), e.g. 'Web, Customer Relation Intern' or
# 'Group IT PMO Intern'. Checked after ENG_RE so exclusion always wins.
NON_ENG_RE = re.compile(
    r"\b(?:customer\s+(?:relation|relations|service)|marketing|growth|sales|"
    r"business\s+development|pmo|project\s+management|product\s+management|"
    r"helpdesk|it\s+support|business\s+analyst|hr|admin|finance|operations)\b",
    re.I,
)
INTERN_RE = re.compile(r"\bintern(?:ship)?\b", re.I)
SENIOR_RE = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|manager|head|director|staff|architect)\b",
    re.I,
)
YEARS_RE = re.compile(r"\b(?:[5-9]|[1-9]\d)\s*\+?\s*years?\b", re.I)
# Location terms accepted by the filter. Override with JOBSEARCH_LOCATION_RE
# (a regex) to target another market, e.g. "\b(?:london|uk|remote)\b".
LOCATION_OK_RE = re.compile(
    os.environ.get("JOBSEARCH_LOCATION_RE", r"\b(?:sg|singapore|remote)\b"), re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def norm_text(s):
    return re.sub(r"\s+", " ", html_mod.unescape(s or "")).strip()


def normalize_url(url):
    """Strip query/fragment and trailing slashes so the same job dedups."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def is_intern_role(role):
    """True only for software-engineering internships.

    Requires an intern token and an engineering token; any non-engineering
    token excludes the role (exclusion wins over inclusion).
    """
    role = role or ""
    if not INTERN_RE.search(role):
        return False
    if not ENG_RE.search(role):
        return False
    if NON_ENG_RE.search(role):
        return False
    if SENIOR_RE.search(role) or YEARS_RE.search(role):
        return False
    return True


def location_ok(loc, assume_sg=False):
    loc = (loc or "").strip()
    if not loc:
        return bool(assume_sg)
    return bool(LOCATION_OK_RE.search(loc))


def parse_posted(text, today):
    """Parse an InternSG-style date badge like '05 Oct' or '05 Oct 2026'."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s*(\d{4})?", text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].title())
    if not mon:
        return None
    day, year = int(m.group(1)), int(m.group(3) or today.year)
    try:
        d = date(year, mon, day)
    except ValueError:
        return None
    if not m.group(3) and d > today + timedelta(days=7):
        try:
            d = date(year - 1, mon, day)
        except ValueError:
            return None
    return d


def posted_ok(posted, today, days=30):
    if posted is None:
        return True
    return (today - timedelta(days=days)) <= posted <= (today + timedelta(days=1))


def is_generic_link(link):
    """True for search/listing URLs that identify no single posting.

    LinkedIn rows captured without their per-posting URL all normalise to the
    same ``/jobs/search`` link, which would pile unrelated postings onto one
    junk dedup key; drop them at import and let the discovery lane recapture
    the canonical ``/jobs/view/<id>`` URL.
    """

    parts = urlsplit(str(link or ""))
    path = parts.path.lower()
    return parts.netloc.lower().endswith("linkedin.com") and (
        path.startswith("/jobs/search") or path.startswith("/jobs/browse"))


def dedup_key(p):
    return "|".join((
        norm_text(p.get("company", "")).lower(),
        norm_text(p.get("role", "")).lower(),
        normalize_url(p.get("link", "")).lower(),
    ))


# ---------------------------------------------------------------- parsing
def _attrs(tag):
    d = {}
    for m in re.finditer(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"', tag):
        d[m.group(1).lower()] = m.group(2)
    return d


def _classed(tag_value, name):
    return name in (tag_value or "").split()


def parse_internsg(html_text):
    """Extract raw postings from an InternSG listing page or the home page."""
    out = []
    # /jobs/ listing rows: title, company, location, date badge, link.
    for m in re.finditer(r"<a\b([^>]*\bjob-listing-row\b[^>]*)>(.*?)</a>",
                         html_text, re.S):
        attrs, body = _attrs(m.group(1)), m.group(2)
        href = attrs.get("href", "")
        title = re.search(r'class="job-listing-title"[^>]*>(.*?)</span>', body, re.S)
        company = re.search(r'class="job-listing-company"[^>]*>(.*?)(?:<span|</div>)',
                           body, re.S)
        loc = re.search(r'class="job-listing-dt"[^>]*>(.*?)</span>', body, re.S)
        posted = re.search(r'class="[^"]*\bbadge-success\b[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
                           body, re.S)
        out.append({
            "company": norm_text(company.group(1)) if company else "",
            "role": norm_text(title.group(1)) if title else "",
            "link": normalize_url(href),
            "location": norm_text(loc.group(1)) if loc else "",
            "posted_raw": norm_text(posted.group(1)) if posted else "",
        })
    # Home page "latest jobs" widget: Company – <a>Role</a> with a date badge.
    for m in re.finditer(r'<div class="ast-row latestJobItem">(.*?)<a\b([^>]*)>(.*?)</a>',
                         html_text, re.S):
        pre, attrs, role = m.group(1), _attrs(m.group(2)), m.group(3)
        href = attrs.get("href", "")
        posted = re.search(r'class="[^"]*\bbadge-secondary\b[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
                           pre, re.S)
        company = re.sub(r"<[^>]+>", " ", pre)
        company = norm_text(company)
        company = re.sub(r"^\d{1,2}\s+[A-Za-z]{3}\s*", "", company).strip()
        company = re.sub(r"[\u2013\u2014-]\s*$", "", company).strip()
        out.append({
            "company": company,
            "role": norm_text(role),
            "link": normalize_url(href),
            "location": "",
            "posted_raw": norm_text(posted.group(1)) if posted else "",
        })
    return out


def parse_company_page(html_text, cfg):
    """Best-effort anchor scraper for a configured company career page."""
    base = cfg.get("url", "")
    keep_links = [s.lower() for s in _as_list(cfg.get("link_contains"))]
    role_hint = None
    if cfg.get("role_contains"):
        role_hint = re.compile("|".join(re.escape(s) for s in _as_list(cfg["role_contains"])), re.I)
    out = []
    seen = set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html_text, re.S):
        attrs = _attrs(m.group(1))
        href = attrs.get("href", "")
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        # Strip inner markup before measuring: ATS widgets (Greenhouse, Workday)
        # wrap the title and its location in nested tags, so the raw anchor text
        # overruns the 120 char cap and a real posting is discarded before any
        # filter sees it. Measured 2026-10-31 on StraitsX: the software intern
        # anchor is 153 chars raw but 62 chars as text.
        text = norm_text(re.sub(r"<[^>]+>", " ", m.group(2)))
        if not text or len(text) > 120:
            continue
        if not is_intern_role(text):
            continue
        if role_hint and not role_hint.search(text):
            continue
        link = normalize_url(urljoin(base, href))
        if keep_links and not any(s in link.lower() for s in keep_links):
            continue
        if link in seen:
            continue
        seen.add(link)
        out.append({
            "company": norm_text(cfg.get("name", "")),
            "role": text,
            "link": link,
            "location": norm_text(cfg.get("location", "")),
            "posted_raw": "",
        })
    return out


# ---------------------------------------------------------------- companies.yaml
def parse_companies(text):
    """Minimal YAML subset: top-level list of mappings, scalar values and
    inline lists. Enough for the documented companies.yaml format."""
    items = []
    cur = None
    for raw in text.splitlines():
        line = _strip_comment(raw)
        s = line.strip()
        if not s or s in ("[]", "---"):
            continue
        if s.startswith("- "):
            cur = {}
            items.append(cur)
            s = s[2:].strip()
            if s:
                _kv(s, cur)
        elif cur is not None and ":" in s:
            _kv(s, cur)
    return items


def _strip_comment(line):
    out, quote = [], None
    for ch in line:
        if ch in "\"'":
            quote = None if quote == ch else (ch if quote is None else quote)
        if ch == "#" and quote is None:
            break
        out.append(ch)
    return "".join(out)


def _kv(s, d):
    k, _, v = s.partition(":")
    d[k.strip()] = _scalar(v.strip())


def _scalar(v):
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [] if not inner else [_unq(x.strip()) for x in inner.split(",")]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return _unq(v)


def _unq(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


# ---------------------------------------------------------------- fetch
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


def _page_urls(src):
    urls = [src]
    if "/jobs" in src:
        base = src.split("#")[0]
        for n in range(2, MAX_PAGES + 1):
            sep = "&" if "?" in base else "?"
            urls.append(f"{base}{sep}page={n}")
    return urls


def collect_internsg(today, log=lambda *a: None):
    raw, errors = [], []
    for name, src in INTERNSG_SOURCES:
        for page, url in enumerate(_page_urls(src), 1):
            if page > 1:
                import time
                time.sleep(PAGE_SLEEP)
            try:
                html_text = fetch(url)
            except Exception as exc:  # network/SSL/HTTP
                errors.append(f"{url}: {exc}")
                log(f"discover: fetch failed {url}: {exc}")
                continue
            for p in parse_internsg(html_text):
                p["source"] = name
                raw.append(p)
    return raw, errors


def collect_boards(today, log=lambda *a: None):
    """Run every ATS board adapter in ``probe_boards.py`` and return its rows.

    The board configs live in ``probe_boards.BOARDS`` so the ATS knowledge
    stays in one file. Rows come back already shaped for ``normalize``
    (company/role/link/source/location[/posted]); the caller's role, location
    and dedupe passes then treat them exactly like InternSG or company rows.

    One board failing must cost nothing but its own line on stderr: each board
    is probed independently, and an import-level failure degrades the whole
    group to a single logged line rather than aborting the lane.
    """
    try:
        import probe_boards
    except Exception as exc:  # noqa: BLE001 - boards are one source of several
        msg = f"board: probe_boards unavailable: {type(exc).__name__}: {exc}"
        log(msg)
        return [], [msg]
    raw, errors = [], []
    for cfg in probe_boards.BOARDS:
        try:
            label, rows, detail = probe_boards.probe(cfg)
        except Exception as exc:  # noqa: BLE001 - an old/incompatible module
            msg = (f"board-error: {cfg.get('name') if isinstance(cfg, dict) else cfg}: "
                   f"probe raised {type(exc).__name__}: {exc}")
            log(msg)
            errors.append(msg)
            continue
        if label == "PROBED":
            log(f"board: {cfg['name']}: {len(rows)} row(s) ({detail})")
            raw += rows
        elif label == "PROBED 0":
            log(f"board-empty: {cfg['name']}: {detail}")
        else:
            msg = f"board-error: {cfg['name']}: {label}: {detail}"
            log(msg)
            errors.append(msg)
    return raw, errors


def collect_companies(cfg_list, today, log=lambda *a: None):
    raw, errors = [], []
    for cfg in cfg_list:
        url = cfg.get("url")
        if not url:
            continue
        try:
            html_text = fetch(url)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log(f"discover: fetch failed {url}: {exc}")
            continue
        for p in parse_company_page(html_text, cfg):
            p["source"] = cfg.get("name", "")
            raw.append(p)
    return raw, errors


# ---------------------------------------------------------------- normalize
def normalize(p, source=None, today=None):
    posted = p.get("posted")
    if isinstance(posted, date):
        posted_iso = posted.isoformat()
    elif isinstance(posted, str) and re.match(r"\d{4}-\d{2}-\d{2}", posted):
        # Board adapters emit ISO dates directly (Workday badge, ATS payloads).
        posted_iso = posted[:10]
    elif p.get("posted_raw"):
        d = parse_posted(p["posted_raw"], today or date.today())
        posted_iso = d.isoformat() if d else None
    else:
        posted_iso = None
    return {
        "company": norm_text(p.get("company", "")),
        "role": norm_text(p.get("role", "")),
        "link": normalize_url(p.get("link", "")),
        "source": norm_text(source if source is not None else p.get("source", "")),
        "location": norm_text(p.get("location", "")),
        "posted": posted_iso,
    }


def filter_postings(postings, today, days=30, assume_sg=False, log=lambda *a: None):
    keep = []
    for p in postings:
        if not is_intern_role(p.get("role", "")):
            continue
        if not location_ok(p.get("location", ""), assume_sg=assume_sg or p.get("source") == "InternSG"):
            continue
        posted = p.get("posted")
        if isinstance(posted, str) and posted:
            posted = datetime.strptime(posted, "%Y-%m-%d").date()
        if not posted_ok(posted, today, days):
            continue
        keep.append(p)
    return keep


def coalesce(postings):
    """Collapse same-key candidates from one run, keeping the richest fields.
    Never touches the store; only the incoming batch."""
    by, order = {}, []
    for p in postings:
        k = dedup_key(p)
        if k not in by:
            by[k] = dict(p)
            order.append(k)
            continue
        cur = by[k]
        for f in ("location", "posted", "company", "role"):
            if not cur.get(f) and p.get(f):
                cur[f] = p[f]
    return [by[k] for k in order]


def role_filter_split(postings):
    """Apply the SWE-only role filter and keep the rejects for reporting.
    Shared by the fetch lane and the --import-json lane."""
    kept, rejected = [], []
    for p in postings:
        (kept if is_intern_role(p.get("role", "")) else rejected).append(p)
    return kept, rejected


def company_role_key(p):
    """Secondary dedupe: same company and role title is the same opening even
    when the URL differs (reposts, /jobs/view/<id> vs generic search URLs)."""
    return "|".join((
        norm_text(p.get("company", "")).lower(),
        norm_text(p.get("role", "")).lower(),
    ))


def merge_postings(existing, incoming, today):
    """Append only postings whose normalised key is not already present.
    Existing entries are never modified or removed. A posting whose
    company+role pair already exists is treated as a duplicate too, so the
    same opening reposted under a different URL is never stored twice.
    Symmetrically, a URL already stored under a different role title
    (re-scraped reposts with tweaked titles) is also a duplicate."""
    seen = set()
    seen_cr = set()
    seen_link = set()
    for p in existing:
        seen.add(p.get("key") or dedup_key(p))
        seen_cr.add(company_role_key(p))
        seen_link.add(normalize_url(p.get("link", "")).lower())
    merged, new = list(existing), []
    for p in incoming:
        k = p.get("key") or dedup_key(p)
        if (not k.strip("|") or k in seen or company_role_key(p) in seen_cr
                or normalize_url(p.get("link", "")).lower() in seen_link):
            continue
        entry = dict(p)
        entry["key"] = k
        entry.setdefault("added", today.isoformat())
        seen.add(k)
        seen_cr.add(company_role_key(p))
        seen_link.add(normalize_url(p.get("link", "")).lower())
        merged.append(entry)
        new.append(entry)
    return merged, new


def import_postings(data, today):
    """Normalise externally supplied postings (e.g. from the LinkedIn lane)."""
    if isinstance(data, dict):
        data = data.get("postings", [])
    out = []
    for p in data or []:
        if is_generic_link(p.get("link")):
            continue
        n = normalize(p, today=today)
        posted = p.get("posted")
        if posted and not n.get("posted"):
            try:
                n["posted"] = datetime.strptime(str(posted)[:10], "%Y-%m-%d").date().isoformat()
            except ValueError:
                pass
        n["source"] = norm_text(p.get("source", "")) or "import"
        out.append(n)
    return out


# ---------------------------------------------------------------- repo store
def _git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", repo, "-c", "credential.helper=!gh auth git-credential", *args],
        capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def ensure_repo(repo_dir, log=lambda *a: None):
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        os.makedirs(os.path.dirname(repo_dir) or ".", exist_ok=True)
        log(f"discover: cloning {REPO_URL} -> {repo_dir}")
        subprocess.run(["git", "clone", REPO_URL, repo_dir],
                       capture_output=True, text=True, check=True)
    _git(repo_dir, "checkout", "main")
    _git(repo_dir, "pull", "--ff-only", "origin", "main")
    return repo_dir


def read_store(repo_dir):
    path = os.path.join(repo_dir, STORE_PATH)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def write_store(repo_dir, postings):
    path = os.path.join(repo_dir, STORE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(postings, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def commit_and_push(repo_dir, new_count, log=lambda *a: None):
    if new_count <= 0:
        return False
    _git(repo_dir, "add", STORE_PATH)
    msg = f"chore(job-search): add {new_count} new posting(s)"
    _git(repo_dir, "-c", "user.name=job-search", 
         "-c", "user.email=job-search@users.noreply.github.com",
         "commit", "-m", msg)
    _git(repo_dir, "push", "origin", "main")
    log(f"discover: pushed {new_count} new posting(s) to main")
    return True


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="do not write/push the repo")
    ap.add_argument("--limit", type=int, default=0, help="max new postings to emit (0 = all)")
    ap.add_argument("--json", action="store_true", help="emit new postings as JSON")
    ap.add_argument("--import-json", metavar="FILE|-",
                    help="merge externally supplied postings (file or '-') instead of fetching")
    ap.add_argument("--companies", default=COMPANIES_FILE, help="companies.yaml path")
    ap.add_argument("--boards", action="store_true",
                    help="also probe the ATS boards in probe_boards.py "
                         "(Workday/Greenhouse/SmartRecruiters/Google); a failing "
                         "board is logged and skipped")
    ap.add_argument("--repo", default=REPO_DIR, help="resume clone path")
    ap.add_argument("--days", type=int, default=30, help="max posting age in days")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr)
    today = datetime.now().date()
    errors = []

    if args.import_json is not None:
        if args.boards:
            # Refuse rather than warn: --boards adds a fetch source and
            # --import-json replaces fetching entirely, so combining them is
            # always a mistake. Silently continuing would leave a lane that
            # looks wired for boards but never runs them.
            log("discover: --boards cannot be combined with --import-json "
                "(the import lane replaces fetching); drop one of them")
            return 2
        if args.import_json == "-":
            data = json.load(sys.stdin)
        else:
            with open(args.import_json, encoding="utf-8") as fh:
                data = json.load(fh)
        incoming = import_postings(data, today)
        # External lanes are assumed to have filtered location already; missing
        # location is accepted, but a non-Singapore location is still dropped.
        assume_sg = True
    else:
        raw = []
        internsg, errs = collect_internsg(today, log)
        errors += errs
        raw += internsg
        cfg_list = []
        if args.companies and os.path.isfile(args.companies):
            with open(args.companies, encoding="utf-8") as fh:
                cfg_list = parse_companies(fh.read())
        company_raw, errs = collect_companies(cfg_list, today, log)
        errors += errs
        raw += company_raw
        if args.boards:
            board_raw, errs = collect_boards(today, log)
            errors += errs
            raw += board_raw
            log(f"discover: boards: {len(board_raw)} raw row(s) "
                f"({len(errs)} board error(s))")
        incoming = [normalize(p, today=today) for p in raw]
        assume_sg = False

    # A generic search/listing URL identifies no single posting; keep those
    # rows out of both lanes before dedup/role filtering counts them.
    generic = [p for p in incoming if is_generic_link(p.get("link"))]
    if generic:
        log(f"discover: dropped {len(generic)} row(s) with a generic search/listing URL")
        incoming = [p for p in incoming if not is_generic_link(p.get("link"))]

    # SWE-only role filter, applied identically to fetched and imported rows.
    incoming, role_rejected = role_filter_split(incoming)
    if args.import_json is not None:
        log(f"discover: import: {len(role_rejected)}/{len(incoming) + len(role_rejected)} "
            f"row(s) rejected by SWE-only role filter ({len(incoming)} kept)")

    filtered = filter_postings(incoming, today, days=args.days, assume_sg=assume_sg, log=log)
    filtered = coalesce(filtered)

    # existing store (read-only in dry-run; clone/pull otherwise)
    if args.dry_run:
        existing = read_store(args.repo) if os.path.isdir(args.repo) else []
    else:
        ensure_repo(args.repo, log)
        existing = read_store(args.repo)

    merged, new = merge_postings(existing, filtered, today)
    if args.limit and args.limit > 0:
        new = new[:args.limit]
        merged = list(existing) + new

    if not args.dry_run and new:
        write_store(args.repo, merged)
        commit_and_push(args.repo, len(new), log)
    elif not args.dry_run and not new:
        log("discover: no new postings")

    if args.json:
        print(json.dumps(new, indent=2, ensure_ascii=False))
    else:
        for p in new:
            print(f"{p['company']} \u00b7 {p['role']} \u00b7 {p['link']}")

    if errors:
        log(f"discover: {len(errors)} source error(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
