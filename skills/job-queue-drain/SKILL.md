---
name: job-queue-drain
description: Enqueue applications from merged tailoring PRs and drain the queue by submitting them on employer sites, stopping at anything ambiguous. Use when a merged PR must become a queued application, when the user asks to apply to a queued role, or when the drain lane runs.
---

# Queue and drain lane

## Approval and identity

A merged `app/*` tailoring PR is the enqueue event and the per-CV approval. The
queue records `approved_at` and `approved_by` from the merge. It does not approve
unseen form answers or override owner skips, caps, or unresolved eligibility.

Enqueue is idempotent by PR and posting identity:

```bash
python3 scripts/queue.py enqueue --repo "$JOBSEARCH_REPO"
python3 scripts/queue.py next --json
```

Queue state defaults to `~/.local/state/job-search-pipeline/queue.yaml` and
must stay outside the checkout. `JOBSEARCH_QUEUE` selects another path.
Private submission snapshots default to
`~/.local/state/job-search-pipeline/submission-snapshots`; set
`JOBSEARCH_SNAPSHOT_ROOT` to select another private directory. It must be
outside both the public checkout and the private resume repository.

## Durable claim gate

Before any external form action, claim the queued item:

```bash
python3 scripts/queue.py claim --pr PR_NUMBER --repo "$JOBSEARCH_REPO" --json
```

Claim reads the unique matching posting from `data/postings.json`, the selected
profile from `data/profile.yaml` (or the exact path passed with `--profile`),
the current `applications.yaml`, and owner skip/cap files. It fetches a fresh JD
from that posting URL, guards it, and runs the shared eligibility evaluator.
Only `eligible` can claim. Unknown requirements, conflicts, missing profile
facts, a duplicate identity, an active owner cap, or an owner skip stop before
an attempt is recorded. `--force` and review notes do not override these gates.
The tracker, owner skip list, and cap file must be readable; an explicit `[]`
records a verified empty list. Missing or empty-unmarked owner state blocks claim.

Success returns an `attempt_id`. The command writes `status: in_progress`,
`attempt_id`, `attempt_state: authorized`, and `submit_gate: durable` atomically
and flushes the state before returning success. The returned ID is required to
record the outcome. A failed claim has no form attempt and must not proceed to
an external form.

## Drain and record

After a successful claim, the person or agent handles the employer site. Use the
selected profile for known answers. Stop at an ambiguous required field,
captcha, uncovered availability or salary question, work-authorisation
judgement, or uncertain employer cap. The queue code does not open or operate
forms.

Before opening or submitting the form, identify the exact selected PDF and
answers JSON. The JSON must contain only form answers; credentials, OTPs, cookies
and credential-like fields are rejected. Prepare the immutable snapshot for the
active attempt:

```bash
python3 scripts/queue.py prepare-snapshot --pr PR_NUMBER \
  --attempt-id ATTEMPT_ID --pdf "$PDF_FILE" --answers "$ANSWERS_FILE" \
  --operator-verification "I checked the selected PDF and answers for this posting."
```

The command stores only hashes and attempt/application references in the queue.
PDF bytes, exact answer JSON bytes, and the manifest stay in the private snapshot
directory. Preparation must succeed before continuing. Changing either selected
file requires preparation again. The manifest records capture time, verified
source commit IDs or explicit `unknown` values, and the operator's material
check. Finalization adds the later confirmation evidence in a new manifest.

Immediately before the irreversible Submit action, verify both the retained
manifest and bytes and the currently selected files with the same paths:

```bash
python3 scripts/queue.py verify-snapshot --pr PR_NUMBER \
  --attempt-id ATTEMPT_ID --pdf "$PDF_FILE" --answers "$ANSWERS_FILE"
```

The verification expires after two minutes. Do not change the selected files
after verification. A mismatch clears the prior verification and requires a
new `prepare-snapshot`, followed by `verify-snapshot` again. The queue's
`status --status submitted` command refuses to record a new submission without
the matching prepared and recently verified snapshot.

After the employer confirms submission, record that external confirmation
against the exact attempt:

```bash
python3 scripts/queue.py status --pr PR_NUMBER --status submitted \
  --attempt-id ATTEMPT_ID --date YYYY-MM-DD --sent "exact submission summary" \
  --confirmation-evidence "employer portal confirmation observed" \
  --route "company ATS"
```

The queue durably records `status: submitted`, the date, sent summary,
confirmation evidence, and pending provenance before it finalizes the manifest
or performs tracker I/O. A snapshot storage failure leaves the attempt submitted
and terminal with an actionable `snapshot_error`; never submit or retry again.
Repair provenance from the retained prepared manifest with:

```bash
python3 scripts/queue.py finalize-snapshot --pr PR_NUMBER \
  --attempt-id ATTEMPT_ID
```

This repair command only finalizes provenance for a recorded submission. It
never submits. Missing historical artifacts stay unknown and are not rebuilt.
Hashes identify retained bytes; they do not prove the employer received them.

A tracker failure leaves the application submitted with `tracker_sync: error`;
repair bookkeeping with:

```bash
python3 scripts/queue.py sync-tracker --pr PR_NUMBER --repo "$JOBSEARCH_REPO"
```

Tracker sync only updates `applications.yaml` bookkeeping. It does not change
submission facts, create historical snapshots, or authorize another form
attempt. Rows from before snapshot support have unknown snapshot provenance. A
submission may be reconciled as submitted with the same terminal behavior.

## Interrupted attempts and reconciliation

An old or incomplete claim becomes `outcome_unknown`; it is never released
automatically. Run:

```bash
python3 scripts/queue.py release-stale
```

To retry, inspect concrete evidence that submission did not occur, then record
the source and the specific observation. For example:

```bash
python3 scripts/queue.py reconcile --pr PR_NUMBER --attempt-id ATTEMPT_ID \
  --result retry --evidence-source "employer portal application history" \
  --evidence-observation "history shows no submission for this posting; it remains a draft"
```

Generic notes or `--force` cannot reopen a submitted or unresolved attempt.
Evidence that confirms submission uses `--result submitted` with `--date`,
`--sent`, and `--confirmation-evidence`; that result is terminal. Reconciliation
keeps an attempt history in the private queue state.

## Stop conditions

- `gh` is unauthenticated or current tracker data is unavailable.
- The posting URL is not unique, the apply page fails, or it redirects to login.
- Eligibility is unknown or conflicting, or current owner skip/cap state blocks
  the posting.
- Two tracker rows share a canonical application identity.
- A claim was interrupted and concrete evidence cannot establish its outcome.
