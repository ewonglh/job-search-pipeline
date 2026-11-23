#!/usr/bin/env python3
"""Synthetic replay, pagination and admission checks for merge intake."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
from datetime import date
from unittest import mock

import queue as q
import tailor


LINK = "https://fixture.invalid/jobs/52"
FILES = [{"path": "variants/fixture-co/resume.tex"}]
JD = "Current internship description. " * 20


def make_case(root, *, added=None, skipped="[]\n", gates="[]\n"):
    repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(repo, "data"), exist_ok=True)
    posting = {"company": "Fixture Co", "role": "Software Engineer Intern",
               "title": "Software Engineer Intern", "source": "Fixture",
               "link": LINK, "added": added or date.today().isoformat()}
    with open(os.path.join(repo, "data", "postings.json"), "w", encoding="utf-8") as fh:
        json.dump([posting], fh)
    skipped_path = os.path.join(root, "skipped.yaml")
    gates_path = os.path.join(root, "gates.yaml")
    with open(skipped_path, "w", encoding="utf-8") as fh:
        fh.write(skipped)
    with open(gates_path, "w", encoding="utf-8") as fh:
        fh.write(gates)
    return {"repo": repo, "queue": os.path.join(root, "queue.yaml"),
            "skipped": skipped_path, "gates": gates_path, "posting": posting}


def pr(number=52, *, link=LINK, branch="app/fixture-co", merged_at="2026-11-22T10:00:00Z",
       merged_by="demo-user"):
    return {"number": number,
            "title": "app(fixture-co): tailored resume — Software Engineer Intern",
            "headRefName": branch, "headRefOid": "head-%s" % number,
            "mergedAt": merged_at, "mergeCommit": {"oid": "merge-%s" % number},
            "mergedBy": {"login": merged_by},
            "url": "https://github.com/demo-user/resume/pull/%s" % number,
            "body": "Posting: " + link}


def run_live(case, prs, *, files=FILES, jd_text=JD, fetch_error=None,
             fetch_side_effect=None, tracker="[]\n"):
    def observed(_log, on_page=None):
        if on_page:
            on_page(prs)
        return prs

    output = io.StringIO()
    fetch_patch = (mock.patch.object(tailor, "fetch_jd", side_effect=fetch_side_effect)
                   if fetch_side_effect else
                   mock.patch.object(tailor, "fetch_jd", side_effect=fetch_error)
                   if fetch_error else
                   mock.patch.object(tailor, "fetch_jd", return_value={"jd_text": jd_text}))
    with mock.patch.object(q, "gh_merged_prs", side_effect=observed), \
            mock.patch.object(q, "gh_pr_files", return_value=files), \
            mock.patch.object(q, "read_main_tracker", return_value=tracker), \
            mock.patch.object(q, "GH_REPO", "demo-user/resume"), \
            mock.patch.object(tailor, "SKIPPED_FILE", case["skipped"]), \
            mock.patch.object(tailor, "GATE_FILE", case["gates"]), \
            fetch_patch, \
            contextlib.redirect_stdout(output):
        result = q.main(["enqueue", "--queue", case["queue"],
                         "--repo", case["repo"]])
    return result, output.getvalue()


def graphql_page(nodes, *, more, cursor=None):
    return {"data": {"repository": {"pullRequests": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}


# A merge on a later GraphQL page is checkpointed and admitted.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    earlier = [pr(number, merged_by="someone-else") for number in range(1, 1001)]
    pages = [graphql_page(earlier[offset:offset + 100], more=True,
                          cursor="cursor-%s" % (offset // 100 + 1))
             for offset in range(0, 1000, 100)]
    pages.append(graphql_page([pr(1001)], more=False, cursor="cursor-11"))
    completed = [subprocess.CompletedProcess([], 0, json.dumps(page), "")
                 for page in pages]
    with mock.patch.object(q, "_run", side_effect=completed), \
            mock.patch.object(q, "gh_pr_files", return_value=FILES), \
            mock.patch.object(q, "read_main_tracker", return_value="[]\n"), \
            mock.patch.object(q, "GH_REPO", "demo-user/resume"), \
            mock.patch.object(tailor, "SKIPPED_FILE", case["skipped"]), \
            mock.patch.object(tailor, "GATE_FILE", case["gates"]), \
            mock.patch.object(tailor, "fetch_jd", return_value={"jd_text": JD}), \
            contextlib.redirect_stdout(io.StringIO()):
        assert q.main(["enqueue", "--queue", case["queue"],
                       "--repo", case["repo"]]) == 0
    assert [row["pr_number"] for row in q.load_queue(case["queue"])] == [1001]
    saved = q.load_merge_intake(case["queue"])["records"]["1001"]
    assert saved["state"] == "admitted" and saved["head"] == {
        "branch": "app/fixture-co", "sha": "head-1001"}


# File lookup failure stays pending and retries successfully on the next run.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    code, _ = run_live(case, [pr()], files=None)
    assert code == 1 and q.load_queue(case["queue"]) == []
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert saved["state"] == "pending"
    assert saved["reason_code"] == "metadata_unavailable"
    code, _ = run_live(case, [pr()], files=FILES)
    assert code == 0 and len(q.load_queue(case["queue"])) == 1


# A crash after queue durability but before outcome completion is handed off by replay.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    complete = q._set_intake_outcome
    def interrupt_after_insert(queue_path, number, state_name, reason_code, reason, **fields):
        if state_name == "admitted" and reason_code == "unknown":
            raise KeyboardInterrupt("synthetic crash after queue insertion")
        return complete(queue_path, number, state_name, reason_code, reason, **fields)
    with mock.patch.object(q, "_set_intake_outcome", side_effect=interrupt_after_insert):
        try:
            run_live(case, [pr()])
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("injected restart boundary was not reached")
    assert [row["pr_number"] for row in q.load_queue(case["queue"])] == [52]
    assert q.load_merge_intake(case["queue"])["records"]["52"]["state"] == "pending"
    with mock.patch.object(q, "gh_merged_prs", side_effect=lambda _log, on_page=None: (
            on_page([pr()]) or [pr()])), \
            mock.patch.object(q, "gh_pr_files", side_effect=AssertionError("should reuse queue")), \
            mock.patch.object(q, "read_main_tracker", side_effect=AssertionError("should not fetch tracker")), \
            mock.patch.object(q, "GH_REPO", "demo-user/resume"), \
            contextlib.redirect_stdout(io.StringIO()):
        assert q.main(["enqueue", "--queue", case["queue"],
                       "--repo", case["repo"]]) == 0
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert saved["state"] == "admitted" and saved["reason_code"] == "already_queued"


# Old postings are excluded before a network JD check.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root, added="2026-01-01")
    with mock.patch("posting_admission.int_setting", return_value=7):
        code, _ = run_live(case, [pr()],
                           fetch_error=AssertionError("age check should run first"))
    assert code == 0 and q.load_queue(case["queue"]) == []
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert saved["state"] == "excluded" and "older than" in saved["reason"]


# A posting that ages out during the JD check is rejected before queue insertion.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    postings_path = os.path.join(case["repo"], "data", "postings.json")
    def age_posting_during_fetch(_posting):
        stale = dict(case["posting"], added="2026-01-01")
        with open(postings_path, "w", encoding="utf-8") as fh:
            json.dump([stale], fh)
        return {"jd_text": JD}
    with mock.patch("posting_admission.int_setting", return_value=7):
        code, _ = run_live(case, [pr()], fetch_side_effect=age_posting_during_fetch)
    assert code == 0 and q.load_queue(case["queue"]) == []
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert saved["state"] == "excluded" and "older than" in saved["reason"]


# An authoritative empty changed-file list is a completed non-tailoring exclusion.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    code, _ = run_live(case, [pr()], files=[])
    assert code == 0 and q.load_queue(case["queue"]) == []
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert saved["state"] == "excluded"
    assert saved["reason_code"] == "not_tailoring_pr"


# Owner skip and explicit closure both block insertion without creating a claim.
with tempfile.TemporaryDirectory() as root:
    skipped = "- link: " + LINK + "\n"
    case = make_case(root, skipped=skipped)
    code, _ = run_live(case, [pr()], jd_text=JD)
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert code == 0 and saved["state"] == "excluded"
    assert "skip list" in saved["reason"]

with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    code, _ = run_live(case, [pr()], jd_text=JD + " This role is no longer accepting applications.")
    saved = q.load_merge_intake(case["queue"])["records"]["52"]
    assert code == 0 and saved["state"] == "excluded"
    assert "explicitly closed" in saved["reason"]


# Fixture dry-run stays offline and writes neither the queue nor its sidecar.
with tempfile.TemporaryDirectory() as root:
    case = make_case(root)
    prs_path = os.path.join(root, "prs.json")
    tracker_path = os.path.join(root, "tracker.yaml")
    fixture = dict(pr(), files=FILES)
    with open(prs_path, "w", encoding="utf-8") as fh:
        json.dump([fixture], fh)
    with open(tracker_path, "w", encoding="utf-8") as fh:
        fh.write("[]\n")
    with mock.patch.object(q, "gh_merged_prs", side_effect=AssertionError("dry-run must stay offline")), \
            contextlib.redirect_stdout(io.StringIO()):
        assert q.main(["enqueue", "--queue", case["queue"], "--repo", case["repo"],
                       "--prs", prs_path, "--tracker", tracker_path, "--dry-run"]) == 0
    assert not os.path.exists(case["queue"])
    assert not os.path.exists(case["queue"] + ".lock")
    assert not os.path.exists(q.merge_intake_path(case["queue"]))

print("test_queue_intake: pagination, pending replay, admission and offline dry-run passed")
