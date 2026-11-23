# Queue migration and rollout

Use this runbook after the queue changes are merged. Keep the deployment
separate from the implementation PR. Record the acceptance evidence on the
operator’s host. Use synthetic inputs for anything shared outside that host.

## Stage the release and upgrade readers first

1. Record the current and candidate code revisions. For each current entry
   point, record the deployed revision and a digest of any deployed code that
   differs from it. Include the scheduler configuration described below.
2. Stage the candidate in a versioned release directory. Inventory every route
   that reads or writes the queue, including manual commands, scheduler jobs,
   recovery, and projection collection. Record their private state bindings and
   locks. Keep queue, intake checkpoint, tracker, owner controls, review state,
   browser ownership records, prepared and final snapshots, and caches outside
   the versioned release. All writers must agree on their state and lock paths.
   A missing queue is an error, not an empty queue. On first setup only, run
   `python3 scripts/queue.py init --queue "$JOBSEARCH_QUEUE"` from the release
   root. It creates an empty queue once and leaves an existing valid queue
   unchanged. Use the same `JOBSEARCH_QUEUE` binding for every reader and writer.
   Implementation: [`scripts/queue.py`](../scripts/queue.py), `cmd_init` line
   2775 and parser line 4111.
3. Run the [offline suites](../CONTRIBUTING.md) and the focused queue, review,
   recovery, scheduler, and projection tests. Complete the compatibility replay
   below. Switch the read-only consumers first, including both the dashboard
   reader and the process that refreshes its cache. Compare source health and
   active, blocked, unresolved, and confirmed-plus-pending-sync counts with the
   baseline. Stop if a reader omits a row, changes terminal status, or loses
   evidence. Keep the old writers active until the reader checks pass. The
   merged #56 reporting code records sanitized discovery and enqueue history
   under `JOBSEARCH_INTAKE_DIR`; compare source fetch/parse coverage, processed
   and accepted counts, enqueue exclusions, and pending metadata. Its cached
   health view is built from a separate projection database for each release.
   During replay, point every source at fake paths, then collect and report.
   `projection.py collect` has no CLI flags for activity and company history,
   so set those two environment paths as well:

   ```sh
   JOBSEARCH_ACTIVITY_STATE="$FAKE_ACTIVITY_STATE" \
   JOBSEARCH_COMPANY_CATALOG="$FAKE_COMPANY_CATALOG" \
   python3 scripts/projection.py collect \
     --repo "$FAKE_REPO" --tracker "$FAKE_TRACKER" \
     --queue "$FAKE_QUEUE" --ledger "$FAKE_LEDGER" \
     --ingest-state "$FAKE_INGEST_STATE" --intake-dir "$FAKE_INTAKE_DIR" \
     --snapshot-root "$FAKE_SNAPSHOT_ROOT" --review-state "$FAKE_REVIEW_STATE" \
     --preflight-state "$FAKE_PREFLIGHT_STATE" --db "$CANDIDATE_DB" \
     --as-of "$FIXED_AS_OF_DATE"
   python3 scripts/projection.py report health --db "$CANDIDATE_DB"
   ```

   See [intake reporting](intake-reporting.md). The collector and report CLI
   are in [`scripts/projection.py`](../scripts/projection.py), lines 1403,
   1654, and 1670; the sanitized history writer is in
   [`scripts/intake_reporting.py`](../scripts/intake_reporting.py), line 110.

## Replay the deployed jobs in isolation

Create separate temporary state directories for the current and candidate
releases. Give both the same fixture inputs, fixed time, ordering policy, and
synthetic owner decisions. Route GitHub and tracker operations through fixtures
or fakes. Use a controlled fake portal for browser actions. Disconnect the
replay from production credentials, outbound delivery, scheduler triggers, and
state paths. A live dry run can still fetch data, so use isolated inputs even
when a command offers `--dry-run`.

Start with [the legacy queue fixture](../scripts/fixtures/queue/queue_existing.yaml)
and the synthetic attempts in [queue recovery tests](../scripts/test_queue_recovery.py).
Include a queued row without new fields, an interrupted row with unknown
submission outcome, and a confirmed row with its existing attempt, date,
confirmation, and snapshot reference. Keep the fixture input immutable. Run the
old reader, the new reader, and the old reader again against the candidate copy
after an upgrade rehearsal. Compare row identities, owner decisions, terminal
status, and the exact recorded evidence before and after. Keep a copy of the
post-upgrade state and prove whether the old writer can round-trip it without
dropping or changing fields. An old reader accepting a file is insufficient
evidence that the old writer can safely save it.

`queue.py enqueue --dry-run` is only a structural preview. It requires both
offline `--prs` and `--tracker` fixtures and a readable queue. It proposes
candidate rows but does not verify merge ownership, posting age, owner skips or
caps, posting closure, or live admission. The preview reports `admitted: 0`,
`admission_gates: not_evaluated`, and `writes: false`; do not count its proposed
candidates as admitted work. A safe fixture example is:

```sh
python3 scripts/queue.py enqueue \
  --queue scripts/fixtures/queue/queue_existing.yaml \
  --dry-run \
  --prs scripts/fixtures/queue/merged_prs.json \
  --tracker scripts/fixtures/queue/applications_merged.yaml \
  --json
```

The preview behavior is implemented by `scripts/queue.py` at `cmd_enqueue`
line 2570 and the structural-preview block at lines 2607 and 2608.

Use separate fake inputs and queue copies for current and candidate decisions.
Record any differences as proposals until all admission checks have been
replayed with fake external responses.

Replay the installed job entry point and arguments against the current fixtures,
then replay the candidate for the same cases. Replace only the external
boundaries with fakes. For an older agent job, capture its selected row and
tool calls against the fake fixtures as well as its turn result. If the old
job cannot be replayed faithfully, record that gap and stop that writer’s
cutover until its behavior can be compared.

| Lane and fixture | Pass | Stop |
| --- | --- | --- |
| Readiness. Same head and evidence, changed head, changed PR requisition, changed profile or posting facts, old or closed posting, model review alone. | Current matching evidence can become ready. Changed, stale, or missing evidence routes to revalidation or exclusion with a reason. Readiness makes zero merge calls. | An unverified or stale decision becomes ready, or a model verdict authorizes merge. |
| Intake. Owner merge on a later page, temporary changed-files failure, repeated observation, verified non-tailoring merge, posting that ages out between review and enqueue. | Incomplete intake stays pending across restart. Catch-up recovers the valid merge once. Verified skips and policy exclusions remain distinguishable and carry a reason. | Metadata failure becomes a completed skip, an authorized merge disappears, a duplicate is inserted, or a policy-excluded posting is queued. |
| Drain. Two eligible rows tied on rank, pre-submit block, confirmed fake submission, successful agent turn without outcome, interruption during a browser action. | CLI and scheduler choose the same row using the same tie-breaker. A known pre-submit block is recorded. Unknown outcomes stay unresolved, stale attempt writes fail, and the browser ownership gate below passes. | Selection differs, a turn exit alone is treated as a submission outcome, or a second agent can act before the first is terminated or fenced. |
| Tracker sync. Restart after durable confirmation with a prepared snapshot, transient tracker failure, repeated recovery, legacy missing artifact, conflicting tracker row. | Direct confirmation and reconciliation use the same recovery path. The terminal submission survives all failures. Recovery finalizes a recoverable artifact and publishes one tracker row. Missing artifacts and conflicts stay visible for review. | Recovery reopens a confirmed application, duplicates or overwrites a conflicting row, drops evidence, or leaves an unobservable backlog. |

For each case log the fixture revision, lane, old and candidate decision and
reason, changed state in the scratch copy, simulated external calls, and pass
or stop. Show both code revisions and schedule digests on the same record.
Label observed differences and check each against the intended policy change
before proceeding. Verify hashes of the immutable fixture inputs before and
after replay. Real application state and destinations must be unreachable from
the replay. Keep reproducible diagnostics to counts and reason codes.

Every new `queue.py claim` requires both `--runner-id` and `--session-ref`,
plus a configured `JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER`. Use a unique runner
identity and a fresh, never-reused session reference for each browser turn. At
claim time the queue resolves the verifier executable to an absolute path and
pins its command and executable SHA-256 in the durable browser owner. At
`finish-attempt`, it rechecks those pins and requires a verifier response bound
to the PR, attempt, runner, session, and fresh challenge. A fenced result must
also prove that in-flight actions are rejected. Lease expiry, a process exit, or
agent prose does not free the browser slot.
Implementation: `scripts/queue.py` pins the verifier at line 595, validates its
termination response at line 646, and creates the claim at line 1969.

The supported claim form is:

```sh
python3 scripts/queue.py claim --next \
  --runner-id "$RUNNER_ID" --session-ref "$SESSION_REF" \
  --repo "$JOBSEARCH_REPO" --queue "$JOBSEARCH_QUEUE" --json
```

Set `JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER` before the first claim. The runner
must use a fresh session reference for each turn and pass the same IDs to every
attempt-scoped queue command.

## Verify the installed trigger and payload

Keep the planned scheduler payload and trigger versioned. Before cutover,
record the intended effective job ID, payload digest, complete trigger and
schedule definition (including timezone), enabled state, working directory,
runtime bindings, session type, timeout, tool scope, and delivery mode. Preserve
installed routing and delivery settings when repointing code. For cron or
systemd, include the installed job or timer definition and effective overrides.
Read back the complete installed configuration while disabled, after editing,
and again after enabling. Require exactly one matching job and a complete
listing. Do not print a payload that may contain private values.

The merged [#55](https://github.com/ewonglh/job-search-pipeline/issues/55)
OpenClaw runner is supported by this release. Follow the
[runner host setup](openclaw-runner.md#host-setup). Before the first claim, set
the absolute OpenClaw binary path and the claim-pinned ownership verifier
command, then run the adapter from the release root:

```sh
export JOBSEARCH_OPENCLAW_BIN="/absolute/path/to/openclaw"
export JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER="/absolute/path/to/scripts/openclaw_ownership.py --openclaw-bin /absolute/path/to/openclaw"
export JOBSEARCH_OPENCLAW_AGENT=main
python3 scripts/openclaw_drain.py run --max-attempts 1
```

The verifier setting is the exact command
`/absolute/path/to/scripts/openclaw_ownership.py --openclaw-bin /absolute/path/to/openclaw`.
The queue invokes it with claim-bound request JSON on stdin during
`finish-attempt`. Keep both paths absolute and fixed so the queue can pin the
helper command and executable digest at claim time. This adapter relies on
`sessions.abort` and the built-in Browser Gateway honoring the turn's abort
signal; a detached browser worker or plugin cannot use this ownership proof.
The supported OpenClaw version and abort behavior are documented in the
[runner ownership verifier notes](openclaw-runner.md#browser-ownership-verifier).

For scheduler read-back, build `$EXPECTED_DIGESTS_FILE` from the intended
configuration with the `expected_digests()` helper in
[`scripts/openclaw_drain.py`](../scripts/openclaw_drain.py). Include SHA-256
digests for `schedule`, `payload`, and `enabled`, plus `agentId`,
`sessionTarget`, and `delivery` when they are part of the intended configuration.
Run the supported checker with the complete cron listing:

```sh
openclaw cron list --all --json --timeout 10000 |
  python3 scripts/openclaw_drain.py verify-readback \
    --job-id "$JOB_ID" --expected "$EXPECTED_DIGESTS_FILE"
```

The checker requires exactly one matching job and a complete listing, compares
the configured digests, and prints only a pass result or mismatch codes. It
does not display the payload. Independently read back any installed fields not
represented by these JSON fields, including effective working directory,
runtime bindings, timeout, or tool scope. Record the installed release revision
and hashes of the effective entry point and verifier files; a branch name or
intended revision does not prove what the job executes. Any unexplained payload,
trigger, schedule, enabled-state, or code mismatch is a stop.

## Activate writers and verify the browser gate

1. Pause each affected scheduled and manual writer for its cutover. Check for
   active jobs or outstanding attempts. Preserve the state and snapshots in an
   owner-only backup made under the relevant locks or with the writers quiescent.
   Verify the planned configuration with the job still disabled.
2. Activate readiness, intake, and tracker recovery using the corresponding pass
   criteria above. Inspect one ordinary scheduled run per lane by its persisted
   reason counts, checkpoint or backlog, and expected revision. For #56, check
   discovery source coverage and distinguish `healthy_empty`, `partial_failure`,
   and `total_failure` outcomes; check enqueue exclusions and pending metadata.
   Pause the lane on a stop condition, keep its durable state, and investigate
   with the replay.
3. Keep the optional drain disabled until the controlled portal proves that the
   runner can prevent a previous attempt from making browser actions. Use only a
   fake portal. Hold a browser action in flight, interrupt the runner, and prove
   a second claim is refused while the owner is unresolved. Then prove the old
   action has terminated, or that the built-in Browser Gateway boundary rejects
   both new and in-flight actions from the aborted session. Verify the queue
   releases ownership only after that evidence and permits the next distinct
   application while the first outcome remains unresolved. On a route with an
   enforceable submit hook, assert the submission-started marker is durable
   before the fake portal receives the submit action. The offline integration
   is in [`scripts/test_openclaw_drain.py`](../scripts/test_openclaw_drain.py);
   its fake portal holds an action across abort and checks that it cannot submit
   after owner release.

Lease expiry, an agent turn’s exit, or a best-effort stop request alone fails
the browser gate. If termination or fencing cannot be observed, keep the slot
occupied and reconcile interrupted work using the
[queue recovery procedure](../skills/job-queue-drain/SKILL.md#interrupted-attempts-and-reconciliation).
Use only the controlled portal for this verification. Keep PR merge, narrative
approval, and new form questions subject to the existing owner controls.

The merged [#54](https://github.com/ewonglh/job-search-pipeline/issues/54)
release adds a single recovery command for all confirmed rows. This invocation
is supported on the current branch; do not run it against an older installed
release until that release contains the command:

```sh
python3 scripts/queue.py recover-submissions \
  --repo "$JOBSEARCH_REPO" --queue "$JOBSEARCH_QUEUE" --json
```

It finalizes recoverable snapshots before retrying the durable tracker outbox;
it never submits an application. The implementation is in
[`scripts/queue.py`](../scripts/queue.py), `cmd_recover_submissions` line 4025
and parser line 4279. Older releases support only the per-row
`finalize-snapshot` and `sync-tracker` commands.

## Roll back without losing newer state

1. Pause the affected writer and check for in-flight work. Apply the browser
   ownership gate if a drain was interrupted.
2. Preserve the latest queue, intake checkpoint, review decisions, ownership
   records, prepared and final snapshots, and tracker sync evidence. Retain the
   pre-cutover backup for comparison. Restoring over newer state can erase a
   real confirmation or allow a duplicate submission.
3. Rehearse the previous release against an isolated copy with every currently
   persisted state. Compare rows and evidence before and after a representative
   write. Resume the old writer only if it preserves all states, including
   pending intake, unresolved attempts, and confirmed submissions with
   pending or conflicting sync. Otherwise leave that writer paused and use a
   compatible reader while resolving the issue.
4. Verify the restored entry point and installed trigger and payload against
   the rollback plan before resuming. Confirm recovery can still find the
   newest pending work and repeat the applicable lane’s pass checks.
