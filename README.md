# Job-search pipeline

A split pipeline for internship applications: deterministic scripts do the
mechanical half, an agent does the judgement half, and a git merge is the
approval gate for every outbound action.

Harness-agnostic. The scripts are plain Python 3 stdlib. Any scheduler can run
them: cron, systemd, OpenClaw, Hermes Agent, Claude Code, or a human typing
commands. See [SETUP.md](SETUP.md) for three worked tracks.

Discovery and ranking currently prioritize Singapore internships, including
InternSG. Their market filters and scoring are starter policy in
`scripts/discover.py` and `scripts/rubric.yaml`; adjust them when targeting a
different market. Account and filesystem defaults use neutral placeholders.

## The core rule

Nothing leaves your machine without a human-approved merge.

1. A script writes a work item, or a script opens a pull request.
2. You read the diff.
3. A review report may mark the PR ready, but only you merge it in GitHub.
4. Only then does a queue item exist, and only then does anything get submitted.

The scripts never submit an application on their own. The agent never merges its
own work. `scripts/automerge.py` checks that a structured review covers the
current PR head and its master, job description, profile, and artifact hashes.
It only reports readiness. The owner merge remains the enqueue authorization
and is recorded by GitHub; narrative approval for cover letters and unseen form
answers stays an owner decision during the drain.

## Six lanes

| Lane | Script | Runs without an agent | What it does |
|---|---|---|---|
| Discover | `discover.py` | yes | Fetch postings from boards, keep new ones, commit to your resume repo |
| Rank | `rank.py` | yes | Scores guarded postings and applies profile-derived eligibility without a model |
| Tailor | `tailor.py` | no, emits work items | Select untracked postings, fetch each JD, resolve the real apply URL |
| Drain | `queue.py` | no | Turn merged `app/*` PRs into a submit queue, oldest first |
| Email ingest | `email_ingest.py` (optional managed adapter) | yes | Bounded read-only IMAP history ingestion into the private evidence ledger |
| Track | `application_identity.py` `recruitment_evidence.py` `outcomes.py` `stages.py` `summary.py` `dashboard.py` | mostly | Canonical joins, private evidence ledger, verified stage machine, digests, local board |

Four of these are pure mechanics. Tailoring and submission need judgement, so
those are where an agent earns its place or you spend your own time.

The split exists because a model is bad at determinism and good at reading a job
description. Put each where it belongs.

Email ingestion is optional. Its offline `import` command reads normalized
message JSON without OpenClaw or mailbox access. The optional managed adapter
uses OpenClaw's supported SDK to resolve a configured SecretRef in memory and
passes it to the read-only Python worker over stdin. It selects folders read
only, fetches with `BODY.PEEK`, and writes sanitized evidence and coverage to the
private ledger. It never edits tracker stages or sends notifications. A caught
up UID cursor is transport progress, not proof that every callback was found.
See [SETUP.md](SETUP.md#email-ingestion) for configuration and commands.

Rank reads availability, degree, study year, graduation, citizenship, work
authorization, credit, school approval and skills from the selected resume
repo's `data/profile.yaml`. Missing or unclear required facts stay `unknown` in
the shortlist and ranking output; explicit conflicts are excluded. JD caching
and the posting guard run before requirement checks, with no extra fetches.
Alternative degree, year and citizenship requirements remain `unknown` for
review. Provenance includes the profile, guarded JD body, role/title metadata
and combined evaluated text hashes.

Tailor uses the same `eligibility.evaluate` decision before emitting a work
item. Explicit conflicts and guarded closure evidence become holds; unknown
requirements, missing source files, and unavailable JDs also stay out of
generation. `--force` bypasses duplicate selection only. Ready work items carry
exact profile and master byte hashes plus the evaluator's guarded JD,
role/title, and combined-text hashes.
Each hold replaces the previous ready item for its posting. The hold list is
refreshed on every writing pass, including recovery with no remaining holds.

Before PR review, run `scripts/cv_checks.py` on the master and generated TeX, then
`scripts/tailor_preflight.py` with the current work item, master, profile,
artifact, freshly retrieved JD text (`--current-jd`) and expected commit head.
The CLI verifies the actual artifact checkout head and committed file bytes.
The structural findings cover only supported
templates, source-entry matching, bullet coverage, numeric claims, team credit,
and chronology; semantic review remains required. The preflight reports
`review_required` when inputs or the optional layout checker are unavailable,
when evidence is stale, or when no matching prior owner-accepted check record
exists. It consumes but never creates accepted records and cannot authorize a
merge or enqueue. See `skills/job-tailor/SKILL.md` for the exact commands.

## Approval gates

| Gate | Blocks | Relaxable |
|---|---|---|
| PR merge | Every submission | No |
| Form pre-screen | Answers you have not given before | No |
| Captcha | Any automated submit | No, solve it or stop |
| Identity documents | Never stored, never filled | No |

## Queue claim and recovery

`queue.py claim --pr N` is the required gate before an external form action. It
matches one queue item to exactly one current posting in
`$JOBSEARCH_REPO/data/postings.json`, fetches and guards a fresh JD from that
posting URL, and evaluates it against the selected profile. The default profile
is `$JOBSEARCH_REPO/data/profile.yaml`; `--profile PATH` selects an exact
alternate file. Current tracker rows and owner skip/cap state are checked too.
The tracker, owner skip list, and cap file must be readable; use an explicit
`[]` to record a verified empty list. Missing state is unresolved and blocks a
claim.
Missing, conflicting, or unknown eligibility cannot be overridden with
`--force` or a generic review note.

A successful claim durably records `attempt_id`, `attempt_state: authorized`,
`submit_gate: durable`, claim time, and eligibility/profile/JD hashes before the
command returns. Use the returned ID when recording confirmation:

```bash
python3 scripts/queue.py status --pr N --status submitted \
  --attempt-id ID --date YYYY-MM-DD --sent "exact submission summary" \
  --confirmation-evidence "employer confirmation observed"
```

This persists the submitted facts and `tracker_sync: pending` before tracker
I/O. `sync-tracker --pr N` repairs only the tracker bookkeeping; it cannot
reopen a submission. Sync state is `pending`, `syncing`, `synced`, or `error`,
with the attempt and sync IDs kept on the queue item.

Stale or interrupted attempts become `outcome_unknown`. `release-stale` does
not make them retryable. `reconcile --result retry` requires a specific evidence
source and observation that establishes non-submission; confirmed submission
reconciliation is terminal. Direct status changes, generic notes, and
`--force` cannot reopen submitted or unresolved attempts. Queue updates use a
cross-process lock and atomic durable writes; tracker network I/O runs outside
that lock.

`SANITIZATION.md` explains why the identity rule is absolute rather than a
default.

## Local health and funnel reports

From `scripts/`, run `python3 projection.py collect` to explicitly read the
configured local tracker, queue, outcome ledger, ingestion state, submission
snapshot metadata, review state and optional preflight records. This writes a
private SQLite cache outside the checkout. It does not contact GitHub, a
mailbox or a model. Sources are full replacements: valid empty input publishes
deletions, interrupted batches resume from committed cursors, and readers
continue seeing the last fully published snapshot while collection is
incomplete. A failed source retains its last good facts while successful
sources can update. The cache keeps at most eight published snapshots plus the
active and resumable source generations. Its default directory is created
with owner-only permissions on POSIX; a configured database directory must
also be private.
Manifest generations retain only confirmed application/attempt bindings and
resume hashes. Manifest read errors retain the last complete generation with
an error; a complete scan can confirm deletion. Cached reads never reopen
manifests, and snapshot/progress metadata is read in one SQLite transaction.
URL fragments and known tracking fields are removed. Query parameters are
limited to numeric/UUID job identifiers (`id`, `job_id`, `jobid`, `gh_jid`,
`req_id`, `requisitionid`); other queries make the source visibly invalid
rather than storing private values or merging distinct posting identities.

`python3 projection.py read` prints the cached versioned snapshot and
`python3 projection.py report compact` prints the shared compact status.
`python3 projection.py report traces` shows the cached run drilldown. Explicit
trace collection is separate: `python3 openclaw_traces.py collect --db
"$JOBSEARCH_PROJECTION_DB"` reads the supported OpenClaw cron history and
publishes only allowlisted run and tool metadata through the same projection.
OpenClaw is version-gated to 2026.10.1 (`e178c39`). Optional trajectory bundles
are read from `JOBSEARCH_OPENCLAW_TRAJECTORY_ROOT`; the importer keeps only tool
IDs, names, timing and result categories. Raw prompts, tool arguments/output,
run summaries and mail content are not stored. Application/artifact joins stay
unknown unless the native source exposes explicit IDs. Native counters stay
separated by provider and cron-run scope; no cost or overlapping totals are
calculated.
`python3 summary.py health --as-of YYYY-MM-DD` shows daily submission and queue
health; `python3 summary.py funnel --as-of YYYY-MM-DD` shows mature 14/28-day
outcome cohorts. Add `--json` for JSON. These read/report commands open the
cache read-only and never collect, rebuild or repair it. Health goals and the
ready buffer accept `--weekday-goal`, `--weekend-goal`, and `--ready-buffer`
(defaults: 1 weekday, 0 weekend, and 1 queued item); `--timezone` sets the
local calendar.

Tracker and queue sources do not declare when their facts were observed, so
their observation time stays unknown and freshness is reported as stale. The
ledger and ingestion sources use explicit observed/scan timestamps. Native
cron history also lacks a separate observation timestamp, so its freshness
remains unknown even after collection;
filesystem modification times never refresh source age. Collection time,
source observation time and snapshot generation time remain separate. A
missing reply is not treated as a confirmed non-response.

Projection compatibility is read-only and allowlisted. Tracker rows accept
`pr` or `pr_number` references and `status` or `stage` values, using the
existing status aliases. Schema-1 evidence ledgers with an `events` list but no
`coverage` block remain readable and report incomplete coverage. Ingestion
accepts the current `scopes` map and the issue-documented legacy `folders` map;
only each entry's `last_scan_at` is projected. Invalid identity rows are
quarantined, invalid optional fields are dropped, and source health reports
`coverage_status`, `coverage_reasons`, `quarantined_count`, and
`redacted_count`. The adapter
contracts are implemented in `scripts/projection.py` (`TRACKER_FIELDS` at
line 43 and source adapters at lines 284, 332, and 481). Tracker aliases come
from `scripts/summary.py` line 98, and the older evidence envelope is accepted
by `scripts/outcomes.py` line 35. The legacy `folders` key is specified in
[issue 30](https://github.com/ewonglh/job-search-pipeline/issues/30). Native
run pages remain pinned to the verified OpenClaw version; unknown page
envelopes report `unsupported_schema` (`scripts/openclaw_traces.py` line 326).

`JOBSEARCH_PREFLIGHT_STATE` optionally points to a private schema-1 JSON file
with a `records` list. Each item contains the result shape returned by
`tailor_preflight.check_artifact` under `report`, and may include a separate
`owner_record`. The cache keeps artifact heads, hash metadata, structured
finding codes and owner acceptance state; it discards messages, summaries,
reviewer names and other free text. Missing provenance stays unknown, and a
model verdict never implies owner acceptance.
Acknowledgements are excluded from hiring outcomes. Text and JSON both expose
identity and malformed-evidence diagnostics.

## Dashboard pages

- Run `python3 dashboard_server.py` from `scripts/`; the default listener is
  `127.0.0.1:8765` ([server defaults](scripts/dashboard_server.py#L10)).
- Open `/jobs/` for Home, Applications, Analytics, Runs, Tailoring and System
  health ([page navigation](scripts/dashboard_shell.py#L5)).
- `/` and `/index.html` remain Home aliases; `/api/snapshot.json` and `/healthz`
  remain read-only endpoints ([routes](scripts/dashboard_server.py#L15)).
- Page requests read the cached projection without collecting source data
  ([cache reader](scripts/dashboard_server.py#L190)).
- Applications opens as a searchable, sortable table with 25 rows per page;
  filters and page choices live in the URL. Board lanes start with 10 cards and
  load independently. Application detail URLs use opaque keys and show only
  recorded evidence and safe HTTP(S) links
  ([query contract](scripts/application_explorer.py#L16),
  [opaque keys](scripts/application_explorer.py#L69),
  [application views](scripts/dashboard.py#L1099)).
- Runs sorts failures and incomplete records first, pages at 20 items, and uses
  opaque keys for sanitized tool timelines
  ([run views](scripts/dashboard.py#L1325),
  [GET routes](scripts/dashboard_server.py#L211)).
- Tailoring keeps model and owner decisions separate; potential mistailor
  cues are explicitly heuristic and missing owner evidence stays unknown
  ([review views](scripts/dashboard.py#L1438),
  [review projection](scripts/dashboard.py#L937)).
- System health explains source failure, stale data, and partial coverage
  before expandable collection metadata
  ([health views](scripts/dashboard.py#L1062)).
- Private deployment, rollback, and phone/desktop acceptance steps are in
  [dashboard operations](docs/dashboard-operations.md#L1).

## Review readiness

From `scripts/`, `python3 automerge.py` reports whether a reviewed tailoring PR
covers its current head, master resume, job description, profile, and artifact
hashes. Missing or stale review evidence, duplicate identities, an owner skip,
an active queue item, a submitted application, or an active application cap
blocks readiness. The helper does not merge, enqueue, or make a model call. Only
the owner's GitHub merge authorizes `queue.py enqueue`, which records the merge
actor and time. Narrative approval remains an owner decision during the drain.
The merger must match the configured resume repository owner. Offline `--prs`
and `--tracker` inputs require `--dry-run` and cannot persist queue approval.

## What this does not do

- Does not auto-submit. A merged PR puts an item in a queue that a human or an
  agent drains, and the drain still stops at captchas and unseen questions.
- Does not bypass bot detection, captchas, or rate limits.
- Does not scrape at scale. Discovery is a handful of boards you name.
- Its discovery and ranking policy currently prioritizes Singapore. See the
  note at the top before adapting it to another market.
- Does not store passports, national IDs, or birthdates. By design, see
  `SANITIZATION.md`.
- Does not promise a job. It reduces the cost of applying carefully.

## Quick start

```bash
git clone https://github.com/ewonglh/job-search-pipeline.git
cd job-search-pipeline
export JOBSEARCH_GH_REPO="your-user/your-resume-repo"
export JOBSEARCH_REPO_URL="https://github.com/your-user/your-resume-repo.git"
DEMO_ROOT="$(mktemp -d)"
export JOBSEARCH_REPO="$DEMO_ROOT/resume"
export JOBSEARCH_QUEUE="$DEMO_ROOT/runtime/queue.yaml"
export JOBSEARCH_SKIPPED="$DEMO_ROOT/runtime/skipped.yaml"
export JOBSEARCH_DEADLINES="$DEMO_ROOT/runtime/deadlines.yaml"
export JOBSEARCH_OUTBOX="$DEMO_ROOT/runtime/outbox"

mkdir -p "$JOBSEARCH_REPO/data" "$DEMO_ROOT/runtime/outbox"
cp demo/repo/data/postings.json "$JOBSEARCH_REPO/data/postings.json"
cp demo/repo/data/profile.yaml "$JOBSEARCH_REPO/data/profile.yaml"
cp demo/repo/applications.yaml "$JOBSEARCH_REPO/applications.yaml"

cd scripts
python3 test_queue.py
python3 test_tailor.py
python3 test_cv_checks.py
python3 test_tailor_preflight.py
python3 test_stages.py
python3 test_dashboard.py
python3 test_config.py
```

`DEMO_ROOT` is outside the checkout. `demo/` contains fictional sample data;
copy it only into a private resume repository. The four required suites are
offline and need Python 3.9 or newer. `test_cv_checks.py` and
`test_tailor_preflight.py` are the focused tailoring checks. No live repository
or credentials are needed for these tests.

## Configuration

Set repository and runtime paths explicitly for scheduled or private use. The
defaults are placeholders and user-local directories, not a personal account
or a repository inside this checkout. Empty path overrides fail immediately.

| Variable | Meaning | Default |
|---|---|---|
| `JOBSEARCH_GH_REPO` | Your resume repo, `user/repo` | `your-user/your-resume-repo` |
| `JOBSEARCH_REPO_URL` | Clone URL for it | `https://github.com/$JOBSEARCH_GH_REPO.git` |
| `JOBSEARCH_REPO` | Private local resume clone | `~/.local/share/job-search-pipeline/resume` |
| `JOBSEARCH_DOCTRINE_REPO` | Optional tailoring doctrine repository | `your-user/your-resume-tailor-doctrine` |
| `JOBSEARCH_COMPANIES` | Optional discovery company list | `scripts/companies.yaml` |
| `JOBSEARCH_CONTACT_URL` | URL placed in the HTTP User-Agent | `https://example.invalid/` |
| `JOBSEARCH_QUEUE` | Private queue state | `~/.local/state/job-search-pipeline/queue.yaml` |
| `JOBSEARCH_SKIPPED` | Private owner-skip state | `~/.local/state/job-search-pipeline/skipped.yaml` |
| `JOBSEARCH_DEADLINES` | Private deadline state | `~/.local/state/job-search-pipeline/deadlines.yaml` |
| `JOBSEARCH_OUTBOX` | Private draft outbox | `~/.local/state/job-search-pipeline/outbox` |
| `JOBSEARCH_WORK` | Private tailoring work items | `~/.local/state/job-search-pipeline/work` |
| `JOBSEARCH_GATES` | Employer gate configuration | `scripts/gates.yaml` |
| `JOBSEARCH_TRACKER` | Tracker path override for `stages.py` | `$JOBSEARCH_REPO/applications.yaml` |
| `JOBSEARCH_RECRUITMENT_LEDGER` | Private, revision-aware outcome ledger | `~/.local/state/job-search-pipeline/recruitment_events.json` |
| `JOBSEARCH_SUMMARY_STATE` | Digest state | `~/.local/state/job-search-pipeline/summary_state.json` |
| `JOBSEARCH_NOTIFY_STATE` | Private optional notification receipt state | `~/.local/state/job-search-pipeline/notify_state.json` |
| `JOBSEARCH_OPENCLAW_BIN` | OpenClaw CLI executable for receipt history | `openclaw` |
| `JOBSEARCH_NOTIFY_JOB_ID` | Native automation job id used for receipt matching | unset |
| `JOBSEARCH_REVIEW_STATE` | Private review evidence | `~/.local/state/job-search-pipeline/review_state.json` |
| `JOBSEARCH_ACTIONS_STATE` | Local action acknowledgements | `~/.local/state/job-search-pipeline/actions-state.json` |
| `JOBSEARCH_RANKINGS` | Ranking output used by reports | `~/.local/state/job-search-pipeline/rankings.json` |
| `JOBSEARCH_DASHBOARD_OUT` | Dashboard output | `~/.local/state/job-search-pipeline/dashboard/index.html` |
| `JOBSEARCH_INGEST_STATE` | Private email-ingestion cursors | `~/.local/state/job-search-pipeline/email_ingest_state.json` |
| `JOBSEARCH_PROJECTION_DB` | Private cached report projection | `~/.local/state/job-search-pipeline/projection/index.sqlite3` |
| `JOBSEARCH_OPENCLAW_TRAJECTORY_ROOT` | Private root of OpenClaw trajectory exports imported by explicit trace collection | unset |
| `JOBSEARCH_TRACE_RETENTION_RECORDS` | Maximum retained native run detail records | `500` (maximum `10000`) |
| `JOBSEARCH_PREFLIGHT_STATE` | Optional content-free preflight records | `~/.local/state/job-search-pipeline/preflight_records.json` |
| `JOBSEARCH_TIMEZONE` | Local day used by health and funnel reports | `Asia/Singapore` |
| `JOBSEARCH_EMAIL_ADDRESS` | Mailbox's own address for outbound detection | unset |
| `JOBSEARCH_EMAIL_FOLDERS` | Comma-separated managed IMAP folders | `INBOX` |

`gh` must be authenticated once (`gh auth login`). Discovery, rank, and queue
read your repository through it. Keep queue, review, work, skip, deadline,
outcome, and draft state outside Git. `JOBSEARCH_REPO` is shared by discovery,
rank, tailoring, queue, stages, summary, dashboard, and the readiness helper.
Import normalized message JSON with `email_ingest.py import` or use its optional
managed IMAP adapter. Review corrections with `outcomes.py`, then use
`stages.py apply-evidence` to propose evidence-backed stage changes. Ingestion
never changes stages or sends mail.

## Setup tracks

Pick by what you already run. Full commands in [SETUP.md](SETUP.md).

| Track | Scheduler | Can run | Needs |
|---|---|---|---|
| 1 | cron | discover, rank, queue | `gh` auth, Python 3 |
| 2 | OpenClaw | core lanes; optional managed email ingestion | OpenClaw gateway, a model |
| 3 | Hermes Agent | core lanes; offline email import | Hermes install, a model |

Track 1 costs nothing and covers the two lanes that find and score work. Tracks 2
and 3 add tailoring and submission, which is where the judgement is.

The `skills/` directory carries no harness name. Point any agent framework at it:
OpenClaw, Hermes, Claude Code, or your own loop. If your framework reads a
different directory convention, copy the five folders there and leave the content
alone.

## Layout

```
scripts/          the pipeline, stdlib only
scripts/fixtures/ synthetic fixtures for the offline tests
demo/             synthetic data mirroring a resume repo layout
skills/           five lane SKILL.md files for the agent tracks
```

## Licence

MIT. See [LICENSE](LICENSE).
