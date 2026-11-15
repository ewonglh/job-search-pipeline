#!/usr/bin/env python3
"""Synthetic offline checks for structural CV preflight findings."""

import json
import subprocess

import cv_checks


MASTER = r"""\resheading{Experience}
\entry{Example Project}{Jan 2024 -- Dec 2024}
Team of 3 engineers.
\begin{itemize}
\item Built 2 services for a sample platform.
\item Improved reliability for sample users.
\end{itemize}
\entry{Sample Lab}{Jan 2025 -- Dec 2025}
Small research group.
\begin{itemize}
\item Prepared synthetic data for analysis.
\item Wrote a reproducible report.
\end{itemize}
"""

VARIANT = r"""\section{Experience}
\resumeSubheading{Sample Lab}{Jan 2025 -- Dec 2025}{Small research group}{}
\resumeItem{Prepared synthetic data for analysis.}
\resumeItem{Wrote a reproducible report.}
\resumeSubheading{Example Project}{Jan 2024 -- Dec 2024}{Team of 3 engineers}{}
\resumeItem{Built 2 services for a sample platform.}
\resumeItem{Improved reliability for sample users.}
"""


clean_findings = cv_checks.check(MASTER, VARIANT)
assert clean_findings == [], clean_findings

unsupported_number = VARIANT.replace("Built 2 services", "Built 7 services")
assert "unsupported_number" in {item["code"] for item in
                                cv_checks.check(MASTER, unsupported_number)}

lost_team_credit = VARIANT.replace("{Team of 3 engineers}{}", "{}{}")
assert "team_attribution" in {item["code"] for item in
                              cv_checks.check(MASTER, lost_team_credit)}

reversed = VARIANT.replace(
    r"\resumeSubheading{Example Project}{Jan 2024 -- Dec 2024}{Team of 3 engineers}{}",
    r"\resumeSubheading{Example Project}{Jan 2025 -- Dec 2025}{Team of 3 engineers}{}").replace(
    r"\resumeSubheading{Sample Lab}{Jan 2025 -- Dec 2025}{Small research group}{}",
    r"\resumeSubheading{Sample Lab}{Jan 2024 -- Dec 2024}{Small research group}{}")
assert "chronology" in {item["code"] for item in cv_checks.check(MASTER, reversed)}

unsupported_template = cv_checks.check("not a supported master", "not a supported variant")
assert [item["code"] for item in unsupported_template] == ["template_unsupported"]

missing_entry = VARIANT.replace("Example Project", "Unmatched Sample")
assert "unmatched_entry" in {item["code"] for item in
                             cv_checks.check(MASTER, missing_entry)}

# Findings identify a position and a stable code without copying resume text.
serialized = json.dumps(cv_checks.check(MASTER, unsupported_number))
assert "Built 7 services" not in serialized
assert "Example Project" not in serialized

unavailable = cv_checks.check_pdf("sample.pdf", which_fn=lambda _name: None)
assert unavailable[0]["code"] == "layout_checker_unavailable", unavailable

pages = cv_checks.check_pdf(
    "sample.pdf", which_fn=lambda _name: "pdfinfo",
    runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
        ["pdfinfo", "sample.pdf"], 0, "Pages: 2\n", ""))
assert pages[0]["code"] == "layout_page_count", pages

print("test_cv_checks: all assertions passed")
