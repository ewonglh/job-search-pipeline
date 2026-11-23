#!/usr/bin/env python3
"""Synthetic checks for exact application identity reconciliation."""
import application_identity as identity
from datetime import date


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

# Contradictory PR fields quarantine every connected strong-ID route.
contradictory = identity.resolve_applications(
    [dict(tracker[0], pr_number=8)],
    queue + [dict(queue[0], link="https://jobs.example/other", pr_number=8)])
assert not contradictory["applications"], contradictory
assert not contradictory["alias_to_canonical"], contradictory
assert contradictory["conflicts"][0]["reason"] == "contradictory_row_prs"

# An explicit alias sharing an exact link is emitted once, with its evidence.
explicit = identity.resolve_applications(tracker, queue, explicit_aliases=[{
    "aliases": ["pr:7", "link:https://jobs.example/role?job_id=42"],
    "canonical_id": "application:reviewed", "reason": "owner confirmed",
    "evidence": "synthetic submission receipt"}])
assert list(explicit["applications"]) == ["application:reviewed"], explicit
assert not explicit["conflicts"], explicit
assert explicit["applications"]["application:reviewed"]["resolution"] == {
    "method": "explicit_alias", "reason": "owner confirmed",
    "evidence": "synthetic submission receipt"}

import discover
import queue as queue_module
import tailor

first = {
    "company": "Fixture Employer", "role": "Software Intern",
    "link": "https://board.example/jobs/first",
    "apply_url": "https://ats.example/Jobs/Aa?req=8",
    "apply_url_evidence": "fixture verified redirect",
    "requisition_id": "fixture:8", "requisition_evidence": "fixture ATS API",
}
second = dict(first, link="https://ats.example/Jobs/aA?req=9", apply_url=None,
              apply_url_evidence=None, requisition_id="fixture:9")
now = date(2026, 11, 22)
store, _ = discover.merge_postings([], [first], now)
store, added = discover.merge_postings(store, [second], now)
assert len(store) == 2 and len(added) == 1
first_slug = tailor.assign_slugs([first])[0][1]
second_slug = tailor.assign_slugs([second])[0][1]
assert first_slug != second_slug
assert [slug for _, slug in tailor.assign_slugs([second, first])] == [second_slug, first_slug]
assert tailor.select_postings([first, second], [first]) == [second]
assert identity.normalize_posting_url(first["apply_url"]) != identity.normalize_posting_url(second["link"])
alias = {"link": first["apply_url"]}
assert discover.merge_postings(store, [alias], now)[1] == []
assert len(discover.coalesce([first, alias, second])) == 2


def merged_pr(number, posting, slug):
    return {
        "number": number,
        "headRefName": f"app/{slug}",
        "title": f"Tailor CV: {posting.get('company', first['company'])} — {first['role']}",
        "body": posting["link"],
        "mergedAt": "2026-11-22T08:00:00Z",
        "files": [{"path": f"variants/{slug}/resume.tex"}],
        "_postings": store,
    }


rows, queued, skips = queue_module.enqueue(
    [], [merged_pr(8, first, first_slug), merged_pr(9, second, second_slug)])
assert len(queued) == 2 and not skips, (queued, skips)
for item, posting in zip(rows, [first, second]):
    queue_module._check_claim_identities(item, rows, [], posting)
assert len(identity.resolve_applications([], rows)["applications"]) == 2

rows[0].update(status="submitted", tracker_sync="pending")
_, queued, skips = queue_module.enqueue(rows, [merged_pr(10, alias, first_slug)])
assert not queued and skips == [(10, "already active for this application")]

print("test_application_identity: all assertions passed")
