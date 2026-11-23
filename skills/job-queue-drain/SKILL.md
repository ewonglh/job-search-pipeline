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

## Durable claim and browser owner

The scheduler selects and reserves the oldest eligible row with one queue
operation. It supplies a unique runner ID and the exact OpenClaw session
reference:

```bash
python3 scripts/queue.py claim --next --runner-id RUNNER_ID \
  --session-ref SESSION_REF --repo "$JOBSEARCH_REPO" --json
```

Every new claim requires both owner values and pins the configured verifier's
absolute command, command digest, and executable digest in the queue row.
`claim --pr N`
is permitted only when PR N is also the shared oldest-first FIFO selection.
The scheduler and direct callers therefore cannot skip ahead by selecting a
different PR. Configure `JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER` in the runner
host environment before claiming; a missing or invalid verifier blocks the
claim. Claim reads the unique matching posting from `data/postings.json`, the selected
profile from `data/profile.yaml` (or the exact path passed with `--profile`),
the current `applications.yaml`, and owner skip/cap files. It fetches a fresh JD
from that posting URL, guards it, and runs the shared eligibility evaluator.
Only `eligible` can claim. Unknown requirements, conflicts, missing profile
facts, a duplicate identity, an active owner cap, or an owner skip stop before
an attempt is recorded. `--force` and review notes do not override these gates.
The tracker, owner skip list, and cap file must be readable; an explicit `[]`
records a verified empty list. Missing or empty-unmarked owner state blocks claim.

Success writes `status: in_progress`, `attempt_id`, `attempt_state: authorized`,
`submit_gate: durable`, runner ID, session reference, verifier command and both
digests, and lease expiry in one locked queue write. The command digest binds
its arguments; the executable digest detects changed executable bytes. The expiry starts recovery;
it never releases the browser slot. A failed claim has no form attempt and must
not proceed to an external form. Pre-existing rows without a verifier pin use
the explicitly configured verifier at recovery time; if it is missing or
uncertain, the slot stays reserved with a clear error.

## Drain and record

After a successful claim, the person or agent handles the employer site. Use the
selected profile for known answers. Stop at an ambiguous required field,
captcha, uncovered availability or salary question, work-authorisation
judgement, or uncertain employer cap. The queue code does not open or operate
forms.

Record a concrete pre-submit blocker against the exact owner tuple:

```bash
python3 scripts/queue.py status --pr PR_NUMBER --status blocked \
  --attempt-id ATTEMPT_ID --runner-id RUNNER_ID --session-ref SESSION_REF \
  --notes "CAPTCHA blocks progress" \
  --evidence-source "employer application form" \
  --evidence-observation "CAPTCHA blocks the form before it can continue."
```

This protects the application from scheduling. The runner still has to verify
termination before the browser slot is free.

Before opening or submitting the form, identify the exact selected PDF and
answers JSON. The JSON must contain only form answers; credentials, OTPs, cookies
and credential-like fields are rejected. Prepare the immutable snapshot for the
active attempt:

```bash
python3 scripts/queue.py prepare-snapshot --pr PR_NUMBER \
  --attempt-id ATTEMPT_ID --runner-id RUNNER_ID --session-ref SESSION_REF \
  --pdf "$PDF_FILE" --answers "$ANSWERS_FILE" \
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
  --attempt-id ATTEMPT_ID --runner-id RUNNER_ID --session-ref SESSION_REF \
  --pdf "$PDF_FILE" --answers "$ANSWERS_FILE"
```

The verification expires after two minutes. Do not change the selected files
after verification. A mismatch clears the prior verification and requires a
new `prepare-snapshot`, followed by `verify-snapshot` again. This is the gate
before Submit. Persist this marker immediately before the irreversible action:

```bash
python3 scripts/queue.py begin-submit --pr PR_NUMBER \
  --attempt-id ATTEMPT_ID --runner-id RUNNER_ID --session-ref SESSION_REF --json
```

After employer confirmation, always record success even if the check has
expired; never rerun a pre-submit check to conceal missing evidence.

After the employer confirms submission, record that external confirmation
against the exact attempt:

```bash
python3 scripts/queue.py status --pr PR_NUMBER --status submitted \
  --attempt-id ATTEMPT_ID --runner-id RUNNER_ID --session-ref SESSION_REF \
  --date YYYY-MM-DD --sent "exact submission summary" \
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
Both confirmation routes finalize retained evidence even with `--no-tracker`.
The queue preserves the original verification timestamp and reports whether it
was fresh, expired, or missing at confirmation. Missing retained materials stay
`unknown`; a missing pre-submit check leaves provenance `incomplete` even if
retained bytes can be finalized. Neither condition authorizes another attempt.

Submission confirmation does not release browser ownership. The runner reads
`browser-owner --json`, verifies the matching process/session independently or
fences new and in-flight browser actions, then calls `finish-attempt` with the
exact PR, attempt, runner, and session tuple. Without a configured independent
verifier, finish fails closed and another browser claim remains blocked. Agent
prose, client exit, stop request, and lease expiry are not termination evidence.
At claim time, `JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER` is resolved to an absolute
command and its executable SHA-256 digest is pinned. At finish, the stored
command is executed after checking the digest; a caller cannot replace it by
changing its environment. For older owner rows with no pin, configure the same
environment variable explicitly for the recovery command. The verifier
receives one JSON request on stdin with schema
`job-search-browser-termination-request/v1`, a fresh challenge, the exact owner
tuple, and the submitted evidence. It must return JSON with schema
`job-search-browser-termination-verdict/v1`,
`verified: true`, the same challenge and owner tuple, a `verifier_id`, and state
`terminated` or `fenced`. A fenced result also requires
`in_flight_rejected: true`. The command must observe or enforce the real browser
boundary; echoing agent data is not verification. Pinning identifies the
configured executable, but its semantics are an operator/deployment property;
it does not defend against host users who can modify the queue or verifier
files. The real browser adapter remains the #55 requirement.

## Interrupted attempts and reconciliation

An old or incomplete claim becomes `outcome_unknown`; the browser owner remains
reserved until independently verified termination. Run:

```bash
python3 scripts/queue.py release-stale
python3 scripts/queue.py browser-owner --json
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

A known pre-submit blocker is recorded as `blocked` with a concrete source and
observation. Browser ownership still must be finished independently. Resume is
explicit and requires evidence that resolves the blocker, with no submit marker
or unknown outcome:

```bash
python3 scripts/queue.py resume --pr PR_NUMBER --attempt-id ATTEMPT_ID \
  --evidence-source "employer application form" \
  --evidence-observation "The consent question was answered and verified."
```

If `begin-submit` was recorded and no confirmation exists, finish records
`outcome_unknown`; use reconciliation, never blocked resume.

## Stop conditions

- `gh` is unauthenticated or current tracker data is unavailable.
- The posting URL is not unique, the apply page fails, or it redirects to login.
- Eligibility is unknown or conflicting, or current owner skip/cap state blocks
  the posting.
- Two tracker rows share a canonical application identity.
- A claim was interrupted and concrete evidence cannot establish its outcome.
