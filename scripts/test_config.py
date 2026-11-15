"""Focused checks for external setup paths and fail-closed configuration."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent


class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jobsearch-config-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        for name in tuple(self.env):
            if name.startswith("JOBSEARCH_"):
                self.env.pop(name)
        self.env.update({"HOME": str(self.home), "USERPROFILE": str(self.home)})

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, name, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args], cwd=str(SCRIPTS),
            env=self.env, text=True, capture_output=True, timeout=30,
        )

    def test_synthetic_resume_repo_can_live_outside_checkout(self):
        repo = self.root / "private-resume"
        shutil.copytree(ROOT / "demo" / "repo", repo)
        runtime = self.root / "private-runtime"
        self.env.update({
            "JOBSEARCH_REPO": str(repo),
            "JOBSEARCH_QUEUE": str(runtime / "queue.yaml"),
            "JOBSEARCH_SKIPPED": str(runtime / "skipped.yaml"),
            "JOBSEARCH_DEADLINES": str(runtime / "deadlines.yaml"),
            "JOBSEARCH_OUTBOX": str(runtime / "outbox"),
            "JOBSEARCH_WORK": str(runtime / "work"),
        })

        tailor = self.run_script("tailor.py", "--list", "--json", "--force")
        self.assertEqual(tailor.returncode, 0, tailor.stderr)
        self.assertIsInstance(json.loads(tailor.stdout), list)
        self.assertIn("candidate(s)", tailor.stderr)

        stages = self.run_script("stages.py", "list", "--json")
        self.assertEqual(stages.returncode, 0, stages.stderr)
        self.assertNotIn("no tracker", stages.stderr.lower())
        self.assertTrue((repo / "applications.yaml").is_file())
        self.assertFalse((runtime / "work").exists())

    def test_empty_repo_override_stops_before_queue_write(self):
        queue = self.root / "queue.yaml"
        original = (
            "- company: Synthetic Example\n"
            "  role: Engineer Intern\n"
            "  link: https://example.invalid/jobs/1\n"
            "  pr: https://github.com/owner/repo/pull/7\n"
            "  pr_number: 7\n"
            "  status: queued\n"
        )
        queue.write_text(original, encoding="utf-8")
        self.env.update({"JOBSEARCH_REPO": "", "JOBSEARCH_QUEUE": str(queue)})

        result = self.run_script("queue.py", "status", "--pr", "7",
                                 "--status", "in_progress", "--no-tracker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration error: JOBSEARCH_REPO", result.stderr)
        self.assertEqual(queue.read_text(encoding="utf-8"), original)

        self.env.update({"JOBSEARCH_REPO": str(self.root / "wrong-repo"),
                         "JOBSEARCH_GH_REPO": "malformed-repository-name"})
        malformed = self.run_script("queue.py", "status", "--pr", "7",
                                    "--status", "in_progress", "--no-tracker")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("JOBSEARCH_GH_REPO must be owner/repository", malformed.stderr)
        self.assertEqual(queue.read_text(encoding="utf-8"), original)

    def test_missing_company_config_stops_before_repo_creation(self):
        repo = self.root / "must-not-be-created"
        missing = self.root / "missing-companies.yaml"
        self.env.update({"JOBSEARCH_REPO": str(repo),
                         "JOBSEARCH_COMPANIES": str(missing)})

        result = self.run_script("discover.py", "--limit", "1")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("companies configuration is missing", result.stderr)
        self.assertFalse(repo.exists())

    def test_malformed_companies_stop_before_network_or_repo_creation(self):
        repo = self.root / "must-not-be-created"
        companies = self.root / "companies.yaml"
        self.env.update({"JOBSEARCH_REPO": str(repo),
                         "JOBSEARCH_COMPANIES": str(companies)})
        cases = [
            "not a company list\n",
            "- name: Synthetic\n  url: https://\n",
            "- name: Synthetic\n  url: https://example.invalid/careers\n  name: Duplicate\n",
            "[]\n- name: Mixed list\n",
        ]
        for content in cases:
            with self.subTest(content=content):
                companies.write_text(content, encoding="utf-8")
                result = self.run_script("discover.py", "--limit", "1")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("companies configuration is invalid", result.stderr)
                self.assertFalse(repo.exists())

    def test_invalid_sleep_stops_before_work_output(self):
        work = self.root / "work"
        self.env["JOBSEARCH_WORK"] = str(work)
        for value in ("nan", "inf", "-0.5"):
            with self.subTest(value=value):
                self.env["JOBSEARCH_TAILOR_SLEEP"] = value
                result = self.run_script("tailor.py", "--list", "--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("JOBSEARCH_TAILOR_SLEEP must be finite and non-negative",
                              result.stderr)
                self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
