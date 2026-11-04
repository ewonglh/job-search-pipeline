---
name: job-track-report
description: Advance application stages from employer email, then report the funnel, hard deadlines and items waiting on the owner. Use when a rejection or interview email needs recording, when the user asks what needs action, or when a nightly digest or daily brief lane runs.
---

# Track and report lane

## What it does

Two read-mostly jobs over the same state:

- **stage tracking**: read employer email, decide rejections and interview
  invitations, and advance the tracker row. `stages.py` edits
  `applications.yaml` line by line, because the file is hand-maintained with
  comments and folded values that a YAML round-trip would destroy.
- **reporting**: a nightly digest of what changed, and a daily action brief of
  what needs the owner.

## Inputs

- `repo/applications.yaml`, the tracker.
- Employer email, via the configured mail lane.
- `deadlines.yaml`, a hand-curated list of hard deadlines.
- `queue.yaml` for in-flight and blocked items.
- `outbox/*.md` for drafts awaiting vet.

## Outputs

- Tracker rows advanced with `stage`, `stage_since`, `stage_source`,
  `stage_evidence`.
- A digest message, or a brief message.
- **Nothing at all** when nothing needs the owner. Silence is the correct output
  for an empty day.

## Procedure: stage from email

1. Classify the message: rejection, interview, assessment, follow-up, unrelated.
2. Match it to exactly one tracker row by company and role.
3. Run `stages.py set --company ... --stage ... --date ... --source email
   --evidence ...`.
4. Never move a row backwards without `--force`, and never mark `Rejected`
   without evidence you can quote.

## Procedure: report

1. Build the brief from deadlines, blocked queue items, stale claims, and drafts.
2. Print only what needs the owner. Number the owner-action rows so one command
   can close one row.
3. Apply the same acknowledgement filter on every render path, or a refresh
   resurrects a row the owner already cleared.

## Traps that cost real time

- **A hand-written deadline list goes stale.** `deadlines.yaml` has no join to the
  tracker, so a completed item keeps rendering as upcoming until someone deletes
  the line. Read it as manual by design and prune it.
- **Mail search results get truncated, and the truncation is silent.** If the
  classifier reads one page of results, a rejection sitting past the page cap is
  misclassified as a follow-up. Page the query, or narrow the date window and
  re-run, rather than trusting the first page.
- **A missing email is not proof nothing arrived.** A sender on a gate list that
  was never read cannot show up in your own search. Say the sender is gated, do
  not conclude the employer never wrote.
- **Marking a row done is not a queue edit.** The ack file is a per-day local
  record, because the queue belongs to the drain lane and would overwrite an edit.

## Idempotency

Re-running a digest for the same day must not duplicate a row or re-notify.
Stage writes are idempotent: setting a row to the stage it already has is a no-op
unless `--force` is passed.

## Stop conditions

- Two tracker rows match one sender: report the ambiguity, do not pick the first.
- The email could not be read: say so. Never infer a rejection from the absence of
  a follow-up.
- A date cannot be parsed: leave the row alone and report it.
