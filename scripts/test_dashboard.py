#!/usr/bin/env python3
"""Offline self-check for dashboard.py: board columns, status/source bucketing,
sankey totals, degraded mode (queue.py + gh down), and the no-PII guarantee.

No network, no repo writes. Run: python3 test_dashboard.py
"""
import datetime as dt
import json
import os
import shutil
import tempfile

import dashboard as d
import summary as s

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = dt.date(2026, 10, 8)
PROFILE_LOCAL = os.path.join(HERE, "profile.local.yaml")

# ---------------------------------------------------------------- fixtures
APPS = s.parse_yaml_list("""
- company: Acme Corp
  role: Backend Engineer Intern
  link: https://example.com/jobs/acme
  status: applied
  applied_date: 2026-10-06
  pr: https://github.com/demo-user/resume/pull/12
- company: Beta Pte Ltd
  role: QA Intern
  link: https://example.com/jobs/beta
  status: Tailored
""")

POSTINGS = [
    # tracked (matches Acme link) -> Applied, not Found
    {"company": "Acme Corp", "role": "Backend Engineer Intern",
     "link": "https://example.com/jobs/acme", "source": "InternSG", "added": "2026-10-05"},
    # covered by the open tailor PR below -> Tailored (via PR)
    {"company": "Iota Inc", "role": "Cloud Intern",
     "link": "https://example.com/jobs/iota", "source": "InternSG", "added": "2026-10-06"},
    # untracked, no PR -> Found
    {"company": "Kappa Ltd", "role": "Sec Intern",
     "link": "https://example.com/jobs/kappa", "source": "InternSG", "added": "2026-10-06"},
    # single-posting obscure source -> sankey bucket "Other"
    {"company": "Tiny Co", "role": "Dev Intern",
     "link": "https://example.com/jobs/tiny", "source": "ObscureBoard", "added": "2026-10-07"},
]

PRS = [
    {"number": 21, "headRefName": "app/iota-inc-cloud-intern",
     "title": "app(iota-inc-cloud-intern): tailored 1-page resume variant — Cloud Intern",
     "body": "posting https://example.com/jobs/iota"},
]

QUEUE = [
    {"company": "Beta Pte Ltd", "role": "QA Intern", "pr_number": 30,
     "pr": "https://github.com/demo-user/resume/pull/30", "status": "failed",
     "enqueued_at": "2026-10-07T01:00:00Z"},
]


# ---------------------------------------------------------------- board
def test_board_columns():
    b = d.build_board(APPS, POSTINGS, PRS, QUEUE, TODAY)
    assert set(b) == set(d.COLUMNS), b.keys()
    # Found: Acme is tracked; Iota is PR-covered; only Kappa and Tiny remain.
    found = sorted(c["company"] for c in b["Found"])
    assert found == ["Kappa Ltd", "Tiny Co"], found
    # Tailored: Beta (tracker) + Iota (PR-matched posting card) = exactly 2.
    tailored = [c["company"] for c in b["Tailored"]]
    assert sorted(tailored) == ["Beta Pte Ltd", "Iota Inc"], tailored
    assert all(c["pr"] for c in b["Tailored"]), b["Tailored"]
    # Iota's card carries the posting's real company/role, with the PR link.
    iota = [c for c in b["Tailored"] if c["company"] == "Iota Inc"][0]
    assert iota["role"] == "Cloud Intern" and iota["pr"].endswith("/pull/21"), iota
    # Applied: Acme from the tracker, exactly once.
    assert [c["company"] for c in b["Applied"]] == ["Acme Corp"], b["Applied"]
    acme = b["Applied"][0]
    assert acme["days"] == (TODAY - dt.date(2026, 10, 6)).days, acme
    assert acme["pr"].endswith("/pull/12"), acme
    # Age for an untracked Found card comes from the posting "added" date.
    tiny = [c for c in b["Found"] if c["company"] == "Tiny Co"][0]
    assert tiny["age_from"] == "added" and tiny["days"] == 1, tiny
    assert {k: len(v) for k, v in b.items()} == {
        "Found": 2, "Tailored": 2, "Applied": 1, "Callback": 0,
        "OA": 0, "Interview": 0, "Accepted": 0, "Rejected": 0}


def test_board_no_gh():
    """prs=None (gh down): tracker-only, Iota falls back to Found."""
    b = d.build_board(APPS, POSTINGS, None, QUEUE, TODAY)
    counts = {k: len(v) for k, v in b.items()}
    assert counts == {"Found": 3, "Tailored": 1, "Applied": 1, "Callback": 0,
                      "OA": 0, "Interview": 0, "Accepted": 0,
                      "Rejected": 0}, counts
    assert [c["company"] for c in b["Tailored"]] == ["Beta Pte Ltd"]
    assert "Iota Inc" in [c["company"] for c in b["Found"]]


def test_board_no_queue():
    """queue=[] degrades: Tailored cards still render, just without queue data."""
    b = d.build_board(APPS, POSTINGS, PRS, [], TODAY)
    counts = {k: len(v) for k, v in b.items()}
    assert counts == {"Found": 2, "Tailored": 2, "Applied": 1, "Callback": 0,
                      "OA": 0, "Interview": 0, "Accepted": 0,
                      "Rejected": 0}, counts


def test_status_bucketing():
    for raw, want in [("to_apply", "Found"), ("tailored", "Tailored"),
                      ("applied", "Applied"), ("interviewing", "Interview"),
                      ("offer", "Accepted"), ("withdrawn", "Rejected"),
                      ("callback", "Callback"), ("oa", "OA"),
                      ("", None), ("nonsense", None)]:
        assert s.canonical_status(raw) == want, (raw, want)
    # Unknown statuses are dropped, never invented into a column.
    b = d.build_board(APPS + [{"company": "Ghost", "role": "X",
                                "status": "nonsense"}],
                      POSTINGS, PRS, QUEUE, TODAY)
    assert all("Ghost" not in c["company"] for cards in b.values() for c in cards)


# ---------------------------------------------------------------- source bucket
def test_source_bucketing():
    postings = ([{"source": "LinkedIn"}] * 3 + [{"source": "InternSG"}] * 2
                + [{"source": "Referral"}])
    assert d.bucket_sources(postings) == {"LinkedIn": 3, "Other": 3}, \
        d.bucket_sources(postings)
    assert d.bucket_sources([{"source": "X"}] * 3) == {"X": 3}
    assert d.bucket_sources([{"source": "X"}] * 2) == {"Other": 2}


# ---------------------------------------------------------------- sankey
def test_sankey():
    links = {}
    b = d.build_board(APPS, POSTINGS, PRS, QUEUE, TODAY, links=links)
    sk = d.build_sankey(POSTINGS, b, status_of=links)
    total = sum(f["count"] for f in sk["flows"])
    assert total == len(POSTINGS), (total, sk)
    by = {(f["from"], f["to"]): f["count"] for f in sk["flows"]}
    assert by.get(("InternSG", "Found")) == 1, by      # Kappa
    assert by.get(("Other", "Found")) == 1, by          # Tiny (1 < 3)
    assert by.get(("InternSG", "Tailored")) == 1, by    # Iota (PR-covered)
    assert by.get(("InternSG", "Applied")) == 1, by     # Acme (tracked)
    # Buckets respect the <3 rule: ObscureBoard collapses, InternSG stays.
    assert sk["left"] == ["InternSG", "Other"], sk["left"]
    assert sk["right"] == ["Found", "Tailored", "Applied"], sk["right"]
    # SVG is self-contained: no external URL, and it opens/closes.
    svg, h = d.render_sankey_svg(sk)
    assert "<svg" in svg and "</svg>" in svg and h > 0 and "http" not in svg


# ---------------------------------------------------------------- degradation
def test_load_queue_failures():
    log = []
    assert d.load_queue(["python3", os.path.join(HERE, "no-such-queue.py")],
                        log=log.append) == []
    assert any("queue check failed" in m for m in log), log
    log2 = []
    assert d.load_queue(["false"], log=log2.append) == []
    assert log2
    log3 = []
    assert d.load_queue(["echo", "not json"], log=log3.append) == []
    assert any("invalid JSON" in m for m in log3), log3


def test_build_data_degraded():
    tmp = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "data"))
        with open(os.path.join(repo, "applications.yaml"), "w", encoding="utf-8") as fh:
            fh.write("# empty tracker\n")
        with open(os.path.join(repo, "data", "postings.json"), "w", encoding="utf-8") as fh:
            json.dump(POSTINGS, fh)
        log = []
        data = d.build_data(repo, queue_cmd=["false"], log=log.append,
                            today=TODAY, prs=None)
        assert data["degraded"] == {"gh": True, "queue": True}, data["degraded"]
        assert data["counts"]["Found"] == len(POSTINGS), data["counts"]
        assert any("no queue" in m for m in log), log
        html = d.render_html(data)
        assert "<svg" in html
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------- PII
def test_no_pii_live():
    """Render the live data; no value from profile.local.yaml may appear."""
    if not os.path.isfile(PROFILE_LOCAL):
        print("test_dashboard: profile.local.yaml absent; live PII check skipped")
        return
    services = os.path.dirname(HERE)
    data = d.build_data(services, log=lambda *a: None, today=TODAY, prs=PRS)
    html = d.render_html(data)
    leaked = []
    with open(PROFILE_LOCAL, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            val = line.partition(":")[2].strip().strip('"').strip("'")
            if len(val) >= 4 and val in html:
                leaked.append(val[:32])
    assert not leaked, f"PII leaked into dashboard HTML: {leaked}"
    for needle in ("Prince George", "118424", "9123 4567", "alex.morgan@example.com",
                   "Alex Morgan", "2005-09-29"):
        assert needle not in html, f"PII in render: {needle}"


def test_no_pii_renderer():
    """The renderer never emits PII from a hostile board object either."""
    data = {"counts": {"Found": 1, "Tailored": 0, "Applied": 0, "Interview": 0,
                       "Offer": 0, "Rejected": 0},
            "board": {k: [] for k in d.COLUMNS},
            "sankey": {"left": [], "right": [], "flows": []},
            "generated": "2026-10-08T00:00:00"}
    html = d.render_html(data)
    assert "118424" not in html and "Prince George" not in html
    assert "dataset.board" in html   # self-contained, minimal JS


# ---------------------------------------------------------------- cli
def test_main_json_and_out():
    tmp = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "data"))
        with open(os.path.join(repo, "applications.yaml"), "w", encoding="utf-8") as fh:
            fh.write("# empty\n")
        with open(os.path.join(repo, "data", "postings.json"), "w", encoding="utf-8") as fh:
            json.dump(POSTINGS, fh)
        out = os.path.join(tmp, "dash", "index.html")

        # Offline: stub gh + queue so no subprocess/network runs.
        d.s.fetch_open_tailor_prs = lambda log=None: PRS
        d.load_queue = lambda cmd=None, log=lambda *a: None: QUEUE

        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = d.main(["--repo", repo, "--out", out, "--json"])
        assert rc == 0
        obj = json.loads(buf.getvalue())
        assert obj["counts"]["Found"] == len(POSTINGS) - 1, obj["counts"]
        assert {"board", "sankey", "flows"} <= set(obj) | set(obj["sankey"])

        with contextlib.redirect_stdout(io.StringIO()):
            rc = d.main(["--repo", repo, "--out", out])
        assert rc == 0 and os.path.isfile(out)
        html = open(out, encoding="utf-8").read()
        assert "Job search dashboard" in html and "<svg" in html
        assert not os.path.exists(out + ".tmp")   # atomic replace cleaned up
    finally:
        shutil.rmtree(tmp)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print("test_dashboard: all assertions passed")


if __name__ == "__main__":
    main()
