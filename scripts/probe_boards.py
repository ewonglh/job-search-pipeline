#!/usr/bin/env python3
"""Big Tech / MNC / quant career-board probe lane (headless, no browser).

Fetches each configured board's public results, either from a server-rendered
data blob (Google) or from the public JSON API behind the ATS widget
(Workday, Greenhouse, SmartRecruiters), and emits the standard posting JSON
for ``discover.py --import-json``.

Boards whose API answers nothing useful are reported as UNPROBED with the
status code and reason on stderr, never silently skipped. A board that
answers 200 with no Singapore rows is ``PROBED 0``: the board works, there is
simply no internship on it right now.

Usage:
  probe_boards.py                 # print postings JSON to stdout
  probe_boards.py --list          # show board configs and probe status
  probe_boards.py --json          # same as default (JSON on stdout)

Output rows: {company, role, link, source, location, posted?}
- Only intern rows (role title contains "intern", case-insensitive) pass the
  role filter downstream; this lane still emits everything matched so the
  existing SWE role filter in discover.py stays the single gatekeeper.
- ``posted`` is omitted when the board carries no parseable date.

Workday failure split (measured 2026-10-31):
- wrong *site* name on a live tenant -> 404 ``Job_Posting_Site_ID=<site>``
  -> reported as BAD-SITE
- tenant that does not resolve / endpoint that rejects the cxs path -> 422
  -> reported as BAD-TENANT
Both mark the board UNPROBED and neither raises.
"""
import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25

WORKDAY_PAGE_LIMIT = 20
WORKDAY_MAX_PAGES = 5
SMARTRECRUITERS_LIMIT = 100
SMARTRECRUITERS_MAX_PAGES = 3

# Location gate used to count "Singapore rows" in --list. discover.py applies
# the same test on import; this copy only drives the status line.
SG_RE = re.compile(r"\b(?:sg|singapore)\b", re.I)


class BoardError(Exception):
    """A board could not be probed. ``label`` is the operator-facing status."""

    def __init__(self, label, detail):
        super().__init__(f"{label}: {detail}")
        self.label = label
        self.detail = detail


# ---------------------------------------------------------------- fetch

def _request(url, data=None):
    """Return (status, headers, body_text). HTTP errors are returned, not raised."""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", "ignore")


def _json_or_raise(status, body, what):
    if status != 200:
        raise BoardError("UNPROBED", f"HTTP {status} from {what}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise BoardError("UNPROBED", f"JSON decode error from {what}: {exc}") from exc


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "ignore")


def _iso_date(value):
    """Return 'YYYY-MM-DD' from an ISO timestamp, else None."""
    if not value:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    return m.group(0) if m else None


def _workday_posted(text, today):
    """Parse Workday's relative badge into an ISO date when unambiguous."""
    text = (text or "").strip()
    if not text:
        return None
    low = text.lower()
    if "today" in low:
        return today.isoformat()
    if "yesterday" in low:
        return (today - dt.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", low)
    if m:
        return (today - dt.timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)),
                           int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        try:
            d = dt.datetime.strptime(
                f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return d.date().isoformat()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- adapters

def google_interns(cfg, today):
    """Google Careers board: q= search over the results page data blob."""
    queries = [
        "software engineering intern",
        "software engineer intern",
        "intern",
    ]
    hits = {}
    for q in queries:
        url = ("https://www.google.com/about/careers/applications/jobs/results/?"
               + urllib.parse.urlencode({"q": q, "location": "Singapore"}))
        html = _get(url)
        m = re.search(r"key: 'ds:1'.*?data:(\[.*)", html, re.S)
        if not m:
            continue
        for jid, title in re.findall(r'\["(\d{15,})","([^"]+?)"', m.group(1)):
            if "intern" not in title.lower():
                continue
            hits[jid] = title
    return [
        {
            "company": "Google",
            "role": title,
            "link": ("https://www.google.com/about/careers/applications/"
                     "jobs/results/" + jid),
            "source": "GoogleCareers",
            "location": "Singapore",
        }
        for jid, title in sorted(hits.items())
    ]


def workday(cfg, today):
    """Workday cxs JSON: POST search, paginate on offset."""
    tenant = cfg["tenant"]
    site = cfg["site"]
    wd = cfg.get("wd", "wd3")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    base_link = f"{host}/en-US/{site}"
    rows, offset = [], 0
    for _ in range(WORKDAY_MAX_PAGES):
        status, _, body = _request(api, {
            "appliedFacets": {}, "limit": WORKDAY_PAGE_LIMIT,
            "offset": offset, "searchText": "intern",
        })
        if status == 404:
            raise BoardError(
                "BAD-SITE",
                f"HTTP 404 from {api} (tenant '{tenant}' exists, site "
                f"'{site}' is wrong; fix the config)")
        if status == 422:
            raise BoardError(
                "BAD-TENANT",
                f"HTTP 422 from {api} (tenant '{tenant}' does not resolve; "
                f"check tenant and wd number)")
        data = _json_or_raise(status, body, api)
        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath") or ""
            if not path:
                continue
            row = {
                "company": cfg["name"],
                "role": p.get("title") or "",
                "link": base_link + path,
                "source": "Workday",
                "location": p.get("locationsText") or "",
            }
            posted = _workday_posted(p.get("postedOn"), today)
            if posted:
                row["posted"] = posted
            rows.append(row)
        total = int(data.get("total") or 0)
        offset += WORKDAY_PAGE_LIMIT
        if not postings or offset >= total:
            break
    return rows


def greenhouse(cfg, today):
    """Greenhouse board API: one GET, no pagination."""
    slug = cfg["slug"]
    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    status, _, body = _request(api)
    if status == 404:
        raise BoardError("BAD-TENANT", f"HTTP 404 from {api} (unknown slug '{slug}')")
    data = _json_or_raise(status, body, api)
    rows = []
    for p in data.get("jobs") or []:
        row = {
            "company": cfg["name"],
            "role": p.get("title") or "",
            "link": p.get("absolute_url") or "",
            "source": "Greenhouse",
            "location": (p.get("location") or {}).get("name") or "",
        }
        posted = _iso_date(p.get("updated_at"))
        if posted:
            row["posted"] = posted
        rows.append(row)
    return rows


def smartrecruiters(cfg, today):
    """SmartRecruiters posting API: paginate on offset while offset+limit < total."""
    slug = cfg["slug"]
    api = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
           f"?limit={SMARTRECRUITERS_LIMIT}")
    rows, offset = [], 0
    for _ in range(SMARTRECRUITERS_MAX_PAGES):
        url = f"{api}&offset={offset}"
        status, _, body = _request(url)
        if status == 404:
            raise BoardError("BAD-TENANT", f"HTTP 404 from {url} (unknown slug '{slug}')")
        data = _json_or_raise(status, body, url)
        content = data.get("content") or []
        for p in content:
            jid = p.get("id")
            if not jid:
                continue
            loc = p.get("location") or {}
            city = (loc.get("city") or "").strip()
            country = (loc.get("country") or "").strip()
            if len(country) == 2:
                country = country.upper()
            row = {
                "company": cfg["name"],
                "role": p.get("name") or "",
                "link": f"https://jobs.smartrecruiters.com/{slug}/{jid}",
                "source": "SmartRecruiters",
                "location": ", ".join(x for x in (city, country) if x),
            }
            posted = _iso_date(p.get("releasedDate"))
            if posted:
                row["posted"] = posted
            rows.append(row)
        total = int(data.get("totalFound") or 0)
        offset += SMARTRECRUITERS_LIMIT
        if not content or offset >= total or offset + SMARTRECRUITERS_LIMIT > total:
            break
    return rows


ADAPTERS = {
    "google": google_interns,
    "workday": workday,
    "greenhouse": greenhouse,
    "smartrecruiters": smartrecruiters,
}

BOARDS = [
    {"name": "Google", "ats": "google"},
    {"name": "CapitaLand Investment", "ats": "workday",
     "tenant": "capitaland", "site": "CapitalandInvestmentCareers"},
    {"name": "Murex", "ats": "workday",
     "tenant": "murex", "site": "MurexCareerPage1"},
    {"name": "Razer", "ats": "workday",
     "tenant": "razer", "site": "Careers"},
    {"name": "StraitsX", "ats": "greenhouse", "slug": "straitsx"},
    {"name": "Grab", "ats": "smartrecruiters", "slug": "Grab"},
    # Candidates still needing a browser lane (no usable public JSON found):
    #   Meta        - careers.facebook.com (JS app)
    #   Jane Street - own board (JS)
    #   Citadel, HRT, Jump, Two Sigma, Optiver, IMC - own boards (JS)
    #   ByteDance family - covered by the existing LinkedIn lane instead.
]


def probe(cfg):
    """Run one board. Returns (label, rows, detail); never raises."""
    fn = ADAPTERS[cfg["ats"]]
    try:
        rows = fn(cfg, dt.date.today())
    except BoardError as exc:
        return exc.label, [], exc.detail
    except Exception as exc:  # noqa: BLE001 - one dead employer must not abort
        return "UNPROBED", [], f"{type(exc).__name__}: {exc}"
    sg = sum(1 for r in rows if SG_RE.search(r.get("location") or ""))
    if sg:
        return "PROBED", rows, f"{len(rows)} rows, {sg} Singapore"
    return "PROBED 0", rows, f"{len(rows)} rows, 0 Singapore"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="show board configs and probe status")
    ap.add_argument("--json", action="store_true",
                    help="emit postings JSON to stdout (default)")
    args = ap.parse_args()

    if args.list:
        for cfg in BOARDS:
            label, _, detail = probe(cfg)
            print(f"{cfg['name']:<22} {cfg['ats']:<15} {label:<9} {detail}")
        return 0

    rows, errors = [], []
    for cfg in BOARDS:
        label, board_rows, detail = probe(cfg)
        if label == "PROBED 0":
            print(f"board-empty: {cfg['name']}: {detail}", file=sys.stderr)
        elif label != "PROBED":
            errors.append(f"{cfg['name']}: {label}: {detail}")
            print(f"board-error: {cfg['name']}: {label}: {detail}", file=sys.stderr)
        rows += board_rows
    json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
