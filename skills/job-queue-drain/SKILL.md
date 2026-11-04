---
name: job-queue-drain
description: Enqueue applications from merged tailoring PRs and drain the queue by submitting them on employer sites, stopping at anything ambiguous. Use when a merged PR must become a queued application, when the user asks to apply to a queued role, or when the drain lane runs.
---

# Queue and drain lane

## The core rule

**A merged `app/*` tailoring PR is the enqueue event.** One queue item per merged
PR. The merge **is** the per-CV approval: the item records `approved_at` from the
PR's `mergedAt` and `approved_by` from `mergedBy`. No separate go-ahead is asked
for that CV.

This is two operations that share one queue file:

- **enqueue**: list merged `app/*` PRs, keep only real tailoring PRs (their diff
  adds or updates `variants/**/resume.tex`), skip ones already queued, append.
- **drain**: take one queued item, fill the employer's form, record the outcome.

## Inputs

- Merged PR list from the tracker repo (needs `gh`, authenticated).
- `queue.yaml`, the queue itself.
- `repo/data/profile.yaml` for standing form answers.
- `profile.local.yaml` for PII, read only when a form has the field, never copied
  anywhere else.
- An offer letter or CV variant path for upload.

## Outputs

- New items in `queue.yaml` with `status: queued`.
- On a drain: `status: applied` plus the submission record, or `status: in_progress`
  with a `notes` blocker, or `status: failed` with the reason.

## Procedure: enqueue

1. List merged `app/*` PRs.
2. Keep only PRs that touch `variants/**/resume.tex`.
3. Skip a PR whose posting link is already recorded in the tracker **or** whose
   link already has a queue item.
4. Append one item per remaining PR, stamping the approval from the merge.

## Procedure: drain

1. Take one `queued` item. Stamp `claimed_at`.
2. Open the employer's apply URL. Prefer the company site or ATS; LinkedIn Easy
   Apply is the fallback when no company listing exists.
3. Fill every field from `profile.yaml`. A field the profile does not cover is a
   stop, not a guess.
4. Upload the variant PDF.
5. Record the outcome. On a submit, the item becomes `applied` and the tracker row
   is written.

## Approval gates

The drain never guesses. Stop and report, leaving the item `in_progress` with a
note, at any of:

- an ambiguous required field
- a captcha
- an availability or salary question the profile does not cover
- a work-authorisation requirement needing a judgement call
- an unverifiable employer cap

Nothing is submitted without a merged variant PR and nothing is submitted without
being recorded in the tracker.

## Idempotency

The enqueue pass must be safe to run every 30 minutes: identical PR list in, no
new items out.

Before submitting, re-read the tracker and skip an item whose company and role
already appear as applied or beyond. This is what stops a second submission of a
role you already applied to.

A claimed item older than a few hours is **abandoned**, not stuck. Release it back
to `queued` with `queue.py release-stale` rather than leaving the drain blocked
forever. A crash during a browser pass leaves exactly this state.

## Stop conditions

- `gh` unauthenticated: stop, do not fall back to an anonymous API call.
- The apply page does not load or redirects to a login: stop and report the URL.
- Two tracker rows match the same company plus role: report the ambiguity instead
  of picking one.
