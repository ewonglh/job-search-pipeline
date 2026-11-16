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
        "current_jd_sha256": contract.sha256_bytes(item["jd_text"].encode("utf-8")),
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
        subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
        with open(os.path.join(repo, "master", "resume.tex"), "w", encoding="utf-8") as fh:
            fh.write("Synthetic resume source.\n")
        with open(os.path.join(repo, "data", "profile.yaml"), "w", encoding="utf-8") as fh:
            fh.write("name: Synthetic Candidate\n")
        with open(os.path.join(repo, "applications.yaml"), "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        with open(os.path.join(repo, "variants", "synthetic-role", "resume.tex"), "w", encoding="utf-8") as fh:
            fh.write("Synthetic tailored resume.\n")
        with open(os.path.join(repo, "variants", "synthetic-role", "resume.pdf"), "wb") as fh:
            fh.write(b"synthetic pdf bytes")
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "add", "master/resume.tex", "data/profile.yaml",
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
        with mock.patch.object(queue_module, "read_main_tracker", return_value="[]\n"):
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

        tracker_row = ("- company: Synthetic Co\n  role: Software Intern\n"
                       "  link: " + LINK + "\n  status: Applied\n")
        active_queue = ("- company: Synthetic Co\n  role: Software Intern\n"
                        "  link: " + LINK + "\n  status: queued\n  pr_number: 12\n")
        # One shared Git fixture exercises the verification seams. Each case
        # changes one input while the review remains internally consistent.
        cases = [
            ("artifact", {}, {}, True, "[]\n", "[]\n", "[]\n", "artifact hash mismatch"),
            ("master", {}, {("rev-parse", "origin/main:master/resume.tex"): "0" * 40},
             False, "[]\n", "[]\n", "[]\n", "master"),
            ("profile", {}, {("show", "origin/main:data/profile.yaml"): "name: Changed Candidate"},
             False, "[]\n", "[]\n", "[]\n", "profile"),
            ("JD", {"jd_text": "A revised job description."}, {}, False,
             "[]\n", "[]\n", "[]\n", "job description"),
            ("malformed queue", {}, {}, False, "not a list\n", "[]\n", "[]\n", "queue"),
            ("active queue", {}, {}, False, active_queue, "[]\n", "[]\n", "active queue"),
            ("malformed tracker", {}, {}, False, "[]\n", "[]\n", "not a list\n", "tracker"),
            ("submitted tracker", {}, {}, False, "[]\n", "[]\n", tracker_row, "already submitted"),
            ("missing cap", {}, {}, False, "[]\n", None, "[]\n", "unavailable"),
            ("malformed cap", {}, {}, False, "[]\n", "- unknown: value\n", "[]\n", "unknown field"),
        ]
        git_output = am._git_output
        blob_digest = am._git_blob_sha256
        for name, updates, git_overrides, bad_artifact, qtext, gtext, tracker, expected in cases:
            with open(queue_file, "w", encoding="utf-8") as fh:
                fh.write(qtext)
            if gtext is None:
                os.remove(gates_file)
            else:
                with open(gates_file, "w", encoding="utf-8") as fh:
                    fh.write(gtext)
            current_item = dict(item, **updates)

            def observed_git(path, *args):
                if args == ("show", "origin/main:applications.yaml"):
                    return tracker
                if args in git_overrides:
                    return git_overrides[args]
                return git_output(path, *args)

            def observed_digest(path, revision, artifact):
                if bad_artifact and artifact.endswith(".pdf"):
                    return "0" * 64
                return blob_digest(path, revision, artifact)

            with mock.patch.object(am, "_git_output", side_effect=observed_git), \
                    mock.patch.object(am, "_git_blob_sha256", side_effect=observed_digest):
                allowed, reason = am.preflight_candidate(
                    pr(head=head), entry, current_item, queue_module=queue_module,
                    tailor_module=tailor_module, queue_file=queue_file,
                    gates_file=gates_file, repo_dir=repo)
            assert not allowed and expected in reason, (name, allowed, reason)


test_main_reports_ready_without_merge()
test_denials()
test_strict_skip_input()
test_duplicate_review_json_is_rejected()
test_provenance_preflight_and_cap()
print("test_automerge: readiness, fail-closed inputs, and owner authority passed")
