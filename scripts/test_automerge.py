#!/usr/bin/env python3
"""Synthetic readiness and fail-closed checks for automerge.py."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import automerge as am
import queue as queue_module
import tailor as tailor_module
import review_contract as contract

HEAD = "1" * 40
LINK = "https://example.invalid/jobs/synthetic-role"


def work_item(link=LINK):
    return {
        "company": "Synthetic Co",
        "role": "Software Intern",
        "link": link,
        "jd_text": "Synthetic job description for a software internship.",
        "profile": {"name": "Synthetic Candidate"},
        "master": "master/resume.tex",
    }


def review(head=HEAD, item=None, master_commit="2" * 40, master_blob="3" * 40,
           artifacts=None):
    item = item or work_item()
    artifacts = artifacts or {
        "variants/synthetic-role/resume.tex": "4" * 64,
        "variants/synthetic-role/resume.pdf": "5" * 64,
    }
    provenance = {
        "schema": contract.SCHEMA,
        "review_head_sha": head,
        "master_commit": master_commit,
        "master_resume_blob": master_blob,
        "master_resume_path": "master/resume.tex",
        **contract.posting_evidence(item, item["jd_text"]),
        "profile_sha256": contract.canonical_sha256(item["profile"]),
        "artifacts": dict(artifacts),
    }
    audit = {
        "truthfulness": "pass",
        "eligibility": "eligible",
        "layout_checked": True,
        "onepage_checked": True,
        "no_tracker_edit": True,
        "findings": [],
        "summary": "Synthetic review evidence is complete.",
        "posting_url": item["link"],
        "current_jd_sha256": provenance["current_jd_sha256"],
        "profile_sha256": provenance["profile_sha256"],
        "master_commit": master_commit,
        "master_resume_blob": master_blob,
        "artifact_sha256": dict(provenance["artifacts"]),
        "checked_at": "2026-11-15T00:00:00Z",
        "provenance_sha256": contract.canonical_sha256(provenance),
    }
    return {"verdict": "approve", "review_head_sha": head,
            "reviewer_version": "synthetic-reviewer/1",
            "provenance": provenance, "audit": audit}


def pr(number=11, head=HEAD, link=LINK, branch="app/synthetic-role"):
    return {"number": number, "title": "app(synthetic-co): Software Intern",
            "headRefName": branch, "headRefOid": head,
            "body": "Posting: " + link, "createdAt": "2026-11-15T00:00:00Z"}


def test_main_reports_ready_without_merge():
    item = work_item()
    with tempfile.TemporaryDirectory() as work_dir:
        with open(os.path.join(work_dir, "synthetic-role.json"), "w", encoding="utf-8") as fh:
            json.dump(item, fh)
        calls, output = [], io.StringIO()
        with mock.patch.object(am, "WORK_DIR", work_dir), \
                mock.patch.object(am, "load_reviewed", return_value={"11": review(item=item)}), \
                mock.patch.object(am, "load_skips", return_value=(set(), set())), \
                mock.patch.object(am, "load_merged_pr_index", return_value={}), \
                mock.patch.object(am, "open_app_prs", return_value=[pr()]), \
                mock.patch.object(am, "preflight_candidate", return_value=(True, "clear")), \
                mock.patch.object(am, "run", side_effect=lambda cmd, timeout=60: calls.append(cmd)), \
                contextlib.redirect_stdout(output):
            result = am.main([])
        assert result == 0
        assert "ready-for-owner-merge #11" in output.getvalue(), output.getvalue()
        assert not any("merge" in [str(part).lower() for part in call] for call in calls), calls
        assert calls == [], calls


def test_invalid_freshness_config_blocks_even_without_candidates():
    error = io.StringIO()
    with mock.patch.dict(os.environ, {"JOBSEARCH_MAX_AGE_DAYS": "invalid"}), \
            mock.patch.object(am, "load_reviewed") as load_reviewed, \
            mock.patch.object(am, "load_skips") as load_skips, \
            mock.patch.object(am, "load_merged_pr_index") as load_merged, \
            mock.patch.object(am, "open_app_prs") as open_prs, \
            contextlib.redirect_stderr(error):
        result = am.main([])
    assert result == 2
    assert "JOBSEARCH_MAX_AGE_DAYS must be an integer" in error.getvalue()
    assert not any(call.called for call in (load_reviewed, load_skips,
                                             load_merged, open_prs))


def test_same_head_requisition_change_requires_revalidation():
    item = dict(work_item(), requisition_id="REQ-41",
                requisition_evidence="synthetic employer requisition record")
    changed = dict(item, requisition_id="REQ-42")
    entry = review(item=item)
    allowed, reason = am.preflight_candidate(pr(), entry, changed)
    assert not allowed and "requisition evidence" in reason, reason

    changed_body = pr()
    changed_body["body"] = "Posting: https://example.invalid/jobs/other-role"
    allowed, reason = am.preflight_candidate(changed_body, review(), work_item())
    assert not allowed and "PR body" in reason, reason


def test_denials():
    base_item = work_item()
    base_pr = pr()
    valid_review = review(item=base_item)
    cases = []

    stale = copy.deepcopy(valid_review)
    stale["review_head_sha"] = "8" * 40
    stale["provenance"]["review_head_sha"] = "8" * 40
    cases.append(("stale head", [base_pr], {"11": stale}, set(), set(),
                  {}, {11: base_item}, "stale PR head"))

    malformed = copy.deepcopy(valid_review)
    malformed["audit"]["artifact_sha256"] = {}
    cases.append(("malformed review", [base_pr], {"11": malformed}, set(), set(),
                  {}, {11: base_item}, "incomplete or malformed"))

    second_item = work_item()
    second_pr = pr(number=12, branch="app/synthetic-role-twin")
    second_review = review(item=second_item)
    cases.append(("duplicate posting", [base_pr, second_pr],
                  {"11": valid_review, "12": second_review}, set(), set(),
                  {}, {11: base_item, 12: second_item}, "same posting identity"))

    cases.append(("owner skip", [base_pr], {"11": valid_review}, set(), {LINK},
                  {}, {11: base_item}, "owner skip"))

    cases.append(("active cap", [base_pr], {"11": valid_review}, set(), set(),
                  {}, {11: base_item}, "application cap", (False, "employer application cap is active")))

    for case in cases:
        name, prs, reviews, skip_prs, skip_links, merged, items, expected = case[:8]
        denied_by_preflight = case[8] if len(case) > 8 else None
        preflight = (lambda *_args, result=denied_by_preflight: result) if denied_by_preflight else (lambda *_args: (True, "clear"))
        results = am.evaluate_readiness(prs, reviews, skip_prs, skip_links,
                                        merged, preflight=preflight,
                                        work_items=items)
        assert all(not row["ready"] for row in results), (name, results)
        assert any(expected in row["reason"] for row in results), (name, results)


def test_strict_skip_input():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "skipped.yaml")
        with mock.patch.object(am, "SKIPPED_YAML", path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("- link: " + LINK + "\n  pr_number: 11\n")
            assert am.load_skips() == ({11}, {LINK})
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("- pr_number: broken\n  link: " + LINK + "\n")
            try:
                am.load_skips()
                raise AssertionError("malformed skip input was accepted")
            except ValueError:
                pass
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("- pr_number: 99\n  link: https://\n")
            try:
                am.load_skips()
                raise AssertionError("malformed URL with a valid PR was accepted")
            except ValueError:
                pass
            os.remove(path)
            try:
                am.load_skips()
                raise AssertionError("missing skip input was accepted")
            except FileNotFoundError:
                pass


def test_duplicate_review_json_is_rejected():
    cases = [
        '{"reviewed":{"11":{"verdict":"reject"},"11":{"verdict":"approve"}}}',
        '{"reviewed":{"11":{"verdict":"reject","verdict":"approve"}}}',
    ]
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "reviews.json")
        with mock.patch.object(am, "REVIEW_STATE", path):
            for raw in cases:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(raw)
                try:
                    am.load_reviewed()
                    raise AssertionError("duplicate JSON keys were accepted")
                except ValueError as exc:
                    assert "duplicate" in str(exc), exc


def test_provenance_preflight_and_cap():
    with tempfile.TemporaryDirectory() as root:
        repo = os.path.join(root, "repo")
        os.makedirs(os.path.join(repo, "master"))
        os.makedirs(os.path.join(repo, "data"))
        os.makedirs(os.path.join(repo, "variants", "synthetic-role"))
        origin = os.path.join(root, "origin.git")
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", origin], check=True)
        with open(os.path.join(repo, "master", "resume.tex"), "w", encoding="utf-8") as fh:
            fh.write("Synthetic resume source.\n")
        with open(os.path.join(repo, "data", "profile.yaml"), "w", encoding="utf-8") as fh:
            fh.write("name: Synthetic Candidate\n")
        with open(os.path.join(repo, "data", "postings.json"), "w", encoding="utf-8") as fh:
            json.dump([{"company": "Synthetic Co", "role": "Software Intern", "link": LINK}], fh)
        with open(os.path.join(repo, "applications.yaml"), "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        with open(os.path.join(repo, "variants", "synthetic-role", "resume.tex"), "w", encoding="utf-8") as fh:
            fh.write("Synthetic tailored resume.\n")
        with open(os.path.join(repo, "variants", "synthetic-role", "resume.pdf"), "wb") as fh:
            fh.write(b"synthetic pdf bytes")
        subprocess.run(["git", "-C", repo, "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", repo, "add", "master/resume.tex", "data/profile.yaml",
                        "data/postings.json",
                        "applications.yaml"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=Synthetic", "-c",
                        "user.email=synthetic@example.invalid", "commit", "-qm", "base fixture"], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", origin], check=True)
        subprocess.run(["git", "-C", repo, "push", "origin", "HEAD:main"],
                       check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
        blob = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD:master/resume.tex"], text=True).strip()
        artifact_paths = ["variants/synthetic-role/resume.tex",
                          "variants/synthetic-role/resume.pdf"]
        subprocess.run(["git", "-C", repo, "add", "variants/synthetic-role"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=Synthetic", "-c",
                        "user.email=synthetic@example.invalid", "commit", "-qm", "PR fixture"], check=True)
        head = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
        artifacts = {path: contract.sha256_bytes(
            subprocess.check_output(["git", "-C", repo, "show", "HEAD:" + path]))
                     for path in artifact_paths}
        subprocess.run(["git", "-C", repo, "push", "origin", "HEAD:refs/pull/11/head"],
                       check=True, capture_output=True)
        queue_file = os.path.join(root, "queue.yaml")
        gates_file = os.path.join(root, "gates.yaml")
        with open(queue_file, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        with open(gates_file, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        item = work_item()
        entry = review(item=item, master_commit=commit, master_blob=blob,
                       head=head, artifacts=artifacts)
        with mock.patch.object(tailor_module, "fetch_jd", return_value=item):
            allowed, reason = am.preflight_candidate(
                pr(head=head), entry, item, queue_module=queue_module,
                tailor_module=tailor_module, queue_file=queue_file,
                gates_file=gates_file, repo_dir=repo)
            assert allowed, reason

        with open(gates_file, "w", encoding="utf-8") as fh:
            fh.write("- company_aliases: [Synthetic Co]\n  gate_until: 2099-01-01\n")
        allowed, reason = am.preflight_candidate(
            pr(head=head), entry, item, queue_module=queue_module,
            tailor_module=tailor_module, queue_file=queue_file,
            gates_file=gates_file, repo_dir=repo)
        assert not allowed and "cap" in reason, reason

        # Another lane records the tracker through its own clone of the origin.
        # The preflight's clone has never fetched it, so a pass here would mean
        # the tracker was read from a stale ref or the working tree.
        lane = os.path.join(root, "lane")
        subprocess.run(["git", "clone", "-q", "--branch", "main", origin, lane], check=True,
                       capture_output=True)
        published = ["[]\n"]

        def publish(text):
            if published[-1] == text:
                return
            with open(os.path.join(lane, "applications.yaml"), "w", encoding="utf-8") as fh:
                fh.write(text)
            subprocess.run(["git", "-C", lane, "add", "applications.yaml"], check=True)
            subprocess.run(["git", "-C", lane, "-c", "user.name=Synthetic", "-c",
                            "user.email=synthetic@example.invalid", "commit", "-qm",
                            "tracker %d" % len(published)], check=True)
            subprocess.run(["git", "-C", lane, "push", "-q", "origin", "HEAD:main"],
                           check=True, capture_output=True)
            published.append(text)

        redirected = dict(item, link="https://example.invalid/jobs/different")
        altered = copy.deepcopy(entry)
        altered["audit"]["posting_url"] = redirected["link"]
        allowed, reason = am.preflight_candidate(
            pr(head=head, link=redirected["link"]), altered, redirected,
            queue_module=queue_module, tailor_module=tailor_module, repo_dir=repo)
        assert not allowed and "requisition evidence" in reason, reason

        tracker_row = ("- company: Synthetic Co\n  role: Software Intern\n"
                       "  link: " + LINK + "\n  status: Applied\n")
        active_queue = ("- company: Synthetic Co\n  role: Software Intern\n"
                        "  link: " + LINK + "\n  status: queued\n  pr_number: 12\n")
        # One shared Git fixture exercises the verification seams. Each case
        # changes one input while the review remains internally consistent.
        cases = [
            ("artifact", {}, {}, True, "[]\n", "[]\n", "[]\n", "artifact hash mismatch"),
            ("master", {}, {("rev-parse", "master/resume.tex"): "0" * 40},
             False, "[]\n", "[]\n", "[]\n", "master"),
            ("profile", {}, {("show", "data/profile.yaml"): "name: Changed Candidate"},
             False, "[]\n", "[]\n", "[]\n", "profile"),
            ("JD", {"jd_text": "A revised job description."}, {}, False,
             "[]\n", "[]\n", "[]\n", "job description"),
            ("aged posting", {}, {("show", commit + ":" + tailor_module.POSTINGS_PATH):
                                  json.dumps([dict(item, added="2000-01-01")])}, False,
             "[]\n", "[]\n", "[]\n", "older than"),
            ("changed live JD", {}, {}, False, "[]\n", "[]\n", "[]\n", "reviewed evidence"),
            ("malformed queue", {}, {}, False, "not a list\n", "[]\n", "[]\n", "queue"),
            ("active queue", {}, {}, False, active_queue, "[]\n", "[]\n", "active queue"),
            ("malformed tracker", {}, {}, False, "[]\n", "[]\n", "not a list\n", "tracker"),
            ("submitted tracker", {}, {}, False, "[]\n", "[]\n", tracker_row, "already submitted"),
            ("missing cap", {}, {}, False, "[]\n", None, "[]\n", "unavailable"),
            ("malformed cap", {}, {}, False, "[]\n", "- unknown: value\n", "[]\n", "unknown field"),
            ("missing queue", {}, {}, False, None, "[]\n", "[]\n", "queue.py init --queue"),
            ("unreadable queue", {}, {}, False, "dir", "[]\n", "[]\n", "malformed or unreadable"),
        ]
        git_output = am._git_output
        blob_digest = am._git_blob_sha256
        posting_read_revisions = []
        for name, updates, git_overrides, bad_artifact, qtext, gtext, tracker, expected in cases:
            if os.path.isdir(queue_file):
                os.rmdir(queue_file)
            elif os.path.exists(queue_file):
                os.remove(queue_file)
            if qtext == "dir":
                os.mkdir(queue_file)
            elif qtext is not None:
                with open(queue_file, "w", encoding="utf-8") as fh:
                    fh.write(qtext)
            if gtext is None:
                if os.path.exists(gates_file):
                    os.remove(gates_file)
            else:
                with open(gates_file, "w", encoding="utf-8") as fh:
                    fh.write(gtext)
            publish(tracker)
            current_item = dict(item, **updates)

            def observed_git(path, *args, git_overrides=git_overrides):
                # Match on command and path so overrides apply to whichever
                # commit the preflight pinned its main reads to.
                key = (args[0], args[1].split(":", 1)[-1]) if len(args) > 1 else None
                exact_key = (args[0], args[1]) if len(args) > 1 else None
                if (args and args[0] == "show" and len(args) > 1
                        and args[1].split(":", 1)[-1] == tailor_module.POSTINGS_PATH):
                    revision = args[1].split(":", 1)[0]
                    assert len(revision) == 40 and all(ch in "0123456789abcdef"
                                                       for ch in revision), args
                    posting_read_revisions.append(revision)
                if exact_key in git_overrides:
                    return git_overrides[exact_key]
                if key in git_overrides:
                    return git_overrides[key]
                return git_output(path, *args)

            def observed_digest(path, revision, artifact):
                if bad_artifact and artifact.endswith(".pdf"):
                    return "0" * 64
                return blob_digest(path, revision, artifact)

            with mock.patch.object(am, "_git_output", side_effect=observed_git), \
                    mock.patch.object(am, "_git_blob_sha256", side_effect=observed_digest), \
                    mock.patch.object(tailor_module, "fetch_jd",
                                      return_value={"jd_text": "Revised live description."}):
                allowed, reason = am.preflight_candidate(
                    pr(head=head), entry, current_item, queue_module=queue_module,
                    tailor_module=tailor_module, queue_file=queue_file,
                    gates_file=gates_file, repo_dir=repo)
            assert not allowed and expected in reason, (name, allowed, reason)

        assert posting_read_revisions, "current postings were not read"

        # Back to a clean, valid state: readiness passes.
        if os.path.isdir(queue_file):
            os.rmdir(queue_file)
        for path in (queue_file, gates_file):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[]\n")
        publish("[]\n")
        args = (pr(head=head), entry, item)
        kwargs = dict(queue_module=queue_module, tailor_module=tailor_module,
                      queue_file=queue_file, gates_file=gates_file, repo_dir=repo)
        with mock.patch.object(tailor_module, "fetch_jd",
                               return_value={"jd_text": item["jd_text"]}):
            allowed, reason = am.preflight_candidate(*args, **kwargs)
        assert allowed, reason

        # A failed refresh blocks readiness even though the clone still holds a
        # readable cached tracker, master resume, and profile.
        cached = subprocess.check_output(
            ["git", "-C", repo, "show", "origin/main:applications.yaml"], text=True)
        assert cached.strip() == "[]"
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin",
                        "file://" + os.path.join(root, "gone-remote.git")], check=True)
        allowed, reason = am.preflight_candidate(*args, **kwargs)
        assert not allowed, reason
        assert "tracker refresh failed" in reason and "cached data was not used" in reason, reason
        refs = subprocess.check_output(
            ["git", "-C", repo, "for-each-ref", "refs/jobsearch"], text=True)
        assert refs == "", refs


test_main_reports_ready_without_merge()
test_invalid_freshness_config_blocks_even_without_candidates()
test_same_head_requisition_change_requires_revalidation()
test_denials()
test_strict_skip_input()
test_duplicate_review_json_is_rejected()
test_provenance_preflight_and_cap()
print("test_automerge: readiness, fail-closed inputs, and owner authority passed")
