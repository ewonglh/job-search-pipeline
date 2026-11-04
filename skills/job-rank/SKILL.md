---
name: job-rank
description: Deterministically score and veto new postings against a rubric, producing a ranked shortlist with full detail. Use when a shortlist is needed before tailoring, when the user asks which postings are worth applying to, or when a scheduled ranking lane runs. Zero tokens, no model call.
---

# Rank lane

## What it does

Selects candidate postings that are not yet tracked, not owner-skipped, and have
no open tailor PR. Fetches each job description over plain HTTP (cached in
`rank_cache.json`), applies hard vetoes, scores survivors against `rubric.yaml`,
and prints a ranked digest on stdout plus full detail in `rankings.json`.

This is the **mechanical** half of fit ranking. It never calls a model and needs
no secrets. A later judgement layer may re-rank the top N; this lane does not.

## Inputs

- `repo/data/postings.json` (posting store, read-only)
- `repo/applications.yaml` (tracker, read-only)
- `skipped.yaml` (owner skips)
- `rubric.yaml` (vetoes and weights)
- Open PR list, to exclude postings already being tailored

## Outputs

- Ranked digest on stdout, one line per posting.
- `rankings.json` with the full scored detail, including every veto reason.
- `rank_cache.json` so a rerun does not refetch the same descriptions.

## Procedure

1. `python3 rank.py --dry-run` previews the shortlist without writing anything.
2. `python3 rank.py` refreshes the cache and writes `rankings.json`.
3. `python3 rank.py --json` for machine-readable output when a downstream lane
   consumes it.

## Idempotency

Deterministic given the same store and rubric. The cache makes a rerun cheap, and
a rerun with an unchanged store produces an identical ranking.

## Stop conditions

- A job description cannot be fetched: the posting is excluded from scoring, not
  scored as zero. Report how many were unfetchable.
- `posting_guard.py` returns a hostile verdict: the posting is skipped, never
  scored. An unreadable or hostile description is a skip, never a guess.
- The rubric is missing or unparseable: stop. Do not invent weights.

## Silence

If no candidates exist, print nothing. A script-only scheduled job with empty
stdout delivers nothing.
