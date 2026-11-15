#!/usr/bin/env python3
"""Synthetic checks for exact application identity reconciliation."""
import application_identity as identity


url_a = "https://JOBS.example/role?job_id=42&utm_source=mail#details"
url_b = "https://jobs.example/role?job_id=43&utm_source=mail"
assert identity.normalize_posting_url(url_a) == "https://jobs.example/role?job_id=42"
assert identity.normalize_posting_url(url_a) != identity.normalize_posting_url(url_b)
assert identity.normalize_posting_url("https://user:pw@jobs.example/role") == ""

tracker = [{"company": "Atlas", "role": "Data Intern", "link": url_a,
            "pr": "https://github.com/demo/resume/pull/7", "applied_date": "2026-01-10"}]
queue = [{"company": "Atlas", "role": "Data Intern",
          "link": "https://jobs.example/role?job_id=42",
          "pr_number": 7, "submitted_at": "2026-01-10T12:00:00Z"}]
joined = identity.resolve_applications(tracker, queue)
assert len(joined["applications"]) == 1
assert next(iter(joined["applications"].values()))["resolution"]["method"] == "exact_posting_link"

# Query-bearing job IDs remain distinct, even for the same employer and role.
ambiguous = identity.resolve_applications(tracker, [dict(queue[0], link=url_b, pr_number=8)])
assert any(item["reason"] == "identity_has_conflicting_strong_ids"
           for item in ambiguous["conflicts"])

# Different roles at one employer are separate exact identities.
roles = identity.resolve_applications(
    [{"company": "Atlas", "role": "Data Intern", "link": "https://jobs.example/a"},
     {"company": "Atlas", "role": "Design Intern", "link": "https://jobs.example/b"}], [])
assert len(roles["applications"]) == 2
assert not roles["conflicts"]

# A unique PR bridges changed ATS/posting URLs only with consistent submission dates.
pr_join = identity.resolve_applications(
    [{"company": "Atlas", "role": "Data Intern", "pr": 9, "applied_date": "2026-01-10"}],
    [{"company": "Atlas", "role": "Data Intern", "pr": 9, "link": "https://jobs.example/new",
      "submitted_at": "2026-01-10"}])
assert len(pr_join["applications"]) == 1
assert next(iter(pr_join["applications"].values()))["resolution"]["method"] == "unique_pr_cross_source"

date_conflict = identity.resolve_applications(
    [{"company": "Atlas", "role": "Data Intern", "link": "https://jobs.example/a",
      "applied_date": "2026-01-10"}],
    [{"company": "Atlas", "role": "Data Intern", "link": "https://jobs.example/a",
      "submitted_at": "2026-01-11"}],
    explicit_aliases=[{"aliases": ["link:https://jobs.example/a",
                                     "identity:Atlas|Data Intern"],
                       "canonical_id": "application:atlas-data", "reason": "review",
                       "evidence": "synthetic conflicting submission dates"}])
assert any(item["reason"] == "explicit_alias_date_conflict"
           for item in date_conflict["conflicts"])

alias_role_conflict = identity.resolve_applications(
    [{"company": "Atlas", "role": "Data Intern", "link": "https://jobs.example/a"}],
    [{"company": "Atlas", "role": "Design Intern", "link": "https://jobs.example/b"}],
    explicit_aliases=[{"aliases": ["link:https://jobs.example/a", "link:https://jobs.example/b"],
                       "canonical_id": "application:reviewed", "reason": "review",
                       "evidence": "synthetic alias evidence"}])
assert any(item["reason"] == "explicit_alias_identity_conflict"
           for item in alias_role_conflict["conflicts"])

print("test_application_identity: all assertions passed")
