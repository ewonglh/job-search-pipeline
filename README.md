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

## Five lanes

| Lane | Script | Runs without an agent | What it does |
|---|---|---|---|
| Discover | `discover.py` | yes | Fetch postings from boards, keep new ones, commit to your resume repo |
| Rank | `rank.py` | yes | Deterministic zero-token scoring against `rubric.yaml`, vetoes hostile postings |
| Tailor | `tailor.py` | no, emits work items | Select untracked postings, fetch each JD, resolve the real apply URL |
| Drain | `queue.py` | no | Turn merged `app/*` PRs into a submit queue, oldest first |
| Track | `application_identity.py` `recruitment_evidence.py` `outcomes.py` `stages.py` `summary.py` `dashboard.py` | mostly | Canonical joins, private evidence ledger, verified stage machine, digests, local board |

Three of these are pure mechanics. Tailoring and submission need judgement, so
those are where an agent earns its place or you spend your own time.

The split exists because a model is bad at determinism and good at reading a job
description. Put each where it belongs.

## Approval gates

| Gate | Blocks | Relaxable |
|---|---|---|
| PR merge | Every submission | No |
| Form pre-screen | Answers you have not given before | No |
| Captcha | Any automated submit | No, solve it or stop |
| Identity documents | Never stored, never filled | No |

`SANITIZATION.md` explains why the identity rule is absolute rather than a
default.

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
python3 test_stages.py
python3 test_dashboard.py
python3 test_config.py
```

`DEMO_ROOT` is outside the checkout. `demo/` contains fictional sample data;
copy it only into a private resume repository. The four required suites are
offline and need Python 3.9 or newer. No live repository or credentials are
needed for these tests.

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
| `JOBSEARCH_REVIEW_STATE` | Private review evidence | `~/.local/state/job-search-pipeline/review_state.json` |
| `JOBSEARCH_ACTIONS_STATE` | Local action acknowledgements | `~/.local/state/job-search-pipeline/actions-state.json` |
| `JOBSEARCH_RANKINGS` | Ranking output used by reports | `~/.local/state/job-search-pipeline/rankings.json` |
| `JOBSEARCH_DASHBOARD_OUT` | Dashboard output | `~/.local/state/job-search-pipeline/dashboard/index.html` |

`gh` must be authenticated once (`gh auth login`). Discovery, rank, and queue
read your repository through it. Keep queue, review, work, skip, deadline,
outcome, and draft state outside Git. `JOBSEARCH_REPO` is shared by discovery, rank,
tailoring, queue, stages, summary, dashboard, and the readiness helper. Import
normalized message JSON with `recruitment_evidence.py`, review corrections with
`outcomes.py`, then use `stages.py apply-evidence` to propose evidence-backed
stage changes. Mailbox access and notifications are outside these commands.

## Setup tracks

Pick by what you already run. Full commands in [SETUP.md](SETUP.md).

| Track | Scheduler | Can run | Needs |
|---|---|---|---|
| 1 | cron | discover, rank, queue | `gh` auth, Python 3 |
| 2 | OpenClaw | all five, agent does tailoring and draining | OpenClaw gateway, a model |
| 3 | Hermes Agent | all five, same split | Hermes install, a model |

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
