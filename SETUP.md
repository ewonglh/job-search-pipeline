# Setup

Three tracks, same pipeline. Pick the one that matches what you already run.

Common prerequisites for all three:

- Python 3.9 or newer, stdlib only. No pip install.
- `git`
- `gh` authenticated once: `gh auth login`
- A resume repo of your own. The scripts read `data/postings.json` and
  `applications.yaml` from a clone, and open PRs against it.

Set these before running a lane. Keep the private resume clone and mutable
runtime state outside the code checkout:

```bash
export JOBSEARCH_GH_REPO="owner/private-resume"
export JOBSEARCH_REPO_URL="https://github.com/owner/private-resume.git"
export JOBSEARCH_REPO="$HOME/.local/share/job-search-pipeline/resume"
export JOBSEARCH_QUEUE="$HOME/.local/state/job-search-pipeline/queue.yaml"
export JOBSEARCH_SNAPSHOT_ROOT="$HOME/.local/state/job-search-pipeline/submission-snapshots"
export JOBSEARCH_SKIPPED="$HOME/.local/state/job-search-pipeline/skipped.yaml"
export JOBSEARCH_DEADLINES="$HOME/.local/state/job-search-pipeline/deadlines.yaml"
export JOBSEARCH_OUTBOX="$HOME/.local/state/job-search-pipeline/outbox"
export JOBSEARCH_WORK="$HOME/.local/state/job-search-pipeline/work"
export JOBSEARCH_SUMMARY_STATE="$HOME/.local/state/job-search-pipeline/summary_state.json"
export JOBSEARCH_REVIEW_STATE="$HOME/.local/state/job-search-pipeline/review_state.json"
export JOBSEARCH_ACTIONS_STATE="$HOME/.local/state/job-search-pipeline/actions-state.json"
export JOBSEARCH_DASHBOARD_OUT="$HOME/.local/state/job-search-pipeline/dashboard/index.html"
mkdir -p "$(dirname "$JOBSEARCH_REPO")" "$(dirname "$JOBSEARCH_QUEUE")" \
  "$JOBSEARCH_SNAPSHOT_ROOT"
mkdir -p "$(dirname "$JOBSEARCH_SUMMARY_STATE")" "$JOBSEARCH_OUTBOX" "$JOBSEARCH_WORK"
```

Use the same values in the scheduler environment. Cron does not inherit your
interactive shell variables. Empty or malformed path overrides stop with a
configuration error instead of resolving to the current directory.

---

## Track 1: plain cron, no agent

The lowest common denominator. Costs nothing, needs no model, and covers the two
lanes that run unattended safely: discovery and ranking.

### What it can run

| Lane | Runs | Why |
|---|---|---|
| Discover | yes | Pure HTTP plus git. No judgement needed |
| Rank | yes | Scores guarded JDs and checks profile-derived eligibility |
| Queue enqueue | yes | Reads merged PRs through `gh`, appends rows |

### What it cannot run

| Lane | Blocked because |
|---|---|
| Tailor | Needs a model to select and compress resume content per posting |
| Drain | Submitting a form needs judgement at captchas and unseen questions |

Tailoring with cron alone means you write the variant yourself. That is a fine
choice, it is just your time instead of a model's.

### Commands

```bash
cd ~/job-search/scripts

./run.sh --limit 20            # discover, merge new postings into your repo
./run-rank.sh --top 10         # score and print a shortlist
./run-queue.sh                 # append newly merged app/* PRs to the queue
```

Each runner is a thin wrapper that cd's to its own directory and calls the
script with your arguments. Read one, they are under 25 lines.

### crontab

```cron
# discovery, every morning
0 8 * * *  cd $HOME/job-search/scripts && JOBSEARCH_GH_REPO=owner/private-resume JOBSEARCH_REPO=$HOME/.local/share/job-search-pipeline/resume JOBSEARCH_REPO_URL=https://github.com/owner/private-resume.git ./run.sh --limit 20 >> $HOME/job-search/discover.log 2>&1

# ranking, after discovery
30 8 * * * cd $HOME/job-search/scripts && JOBSEARCH_GH_REPO=owner/private-resume JOBSEARCH_REPO=$HOME/.local/share/job-search-pipeline/resume ./run-rank.sh --top 10 >> $HOME/job-search/rank.log 2>&1

# queue enqueue, hourly, cheap and silent when nothing merged
15 * * * * cd $HOME/job-search/scripts && JOBSEARCH_GH_REPO=owner/private-resume JOBSEARCH_REPO=$HOME/.local/share/job-search-pipeline/resume JOBSEARCH_QUEUE=$HOME/.local/state/job-search-pipeline/queue.yaml ./run-queue.sh >> $HOME/job-search/queue.log 2>&1
```

Cron does not inherit your shell environment, so each line supplies the repo
settings it needs. Add runtime path overrides to each scheduled command when
they differ from the user-local defaults.

To read the shortlist, cron output goes to a log. Have `run-rank.sh` mail it, or
read the log, or let Track 2 or 3 send it to you.

---

## Track 2: OpenClaw

Covers all five lanes. The gateway schedules the mechanical jobs, and the agent
does the tailoring and draining under the same approval gates.

### Why OpenClaw fits

- Its scheduler has four payload kinds, and the two you want here are `command`
  (shell, no model call) and `message` (a model-backed turn).
  Source: `docs/automation/cron-jobs/payloads.md`, the payload table.
- Command payloads mean the mechanical lanes cost no tokens at all.
- Skills are plain directories the agent reads, so the five lane skills drop in
  without packaging.

### Install and point it at the pipeline

```bash
# from the OpenClaw docs, install track
curl -fsSL https://openclaw.ai/install.sh | bash     # or your preferred method

# skills live in a workspace directory
mkdir -p ~/.openclaw/workspace/job-search/skills
cp -r ./skills/* ~/.openclaw/workspace/job-search/skills/
```

Then tell the agent where the scripts are by making the paths in each
`SKILL.md` absolute, or by exporting `JOBSEARCH_REPO` in the gateway's
environment. A scheduled agent turn does not read your interactive shell rc.

### Scheduling

Mechanical lanes use `--command`, so they cost no tokens:

```bash
openclaw automations create "0 8 * * *" \
  --name "job discover" --tz Asia/Singapore \
  --command "./run.sh --limit 20" \
  --command-cwd "$HOME/job-search/scripts"

openclaw automations create "30 8 * * *" \
  --name "job rank" --tz Asia/Singapore \
  --command "./run-rank.sh --top 10" \
  --command-cwd "$HOME/job-search/scripts"

openclaw automations create "15 * * * *" \
  --name "job queue" --tz Asia/Singapore \
  --command "./run-queue.sh" \
  --command-cwd "$HOME/job-search/scripts"
```

Judgement lanes use `--message`, which starts a model-backed turn:

```bash
openclaw automations create "0 9 * * *" \
  --name "job tailor" --tz Asia/Singapore \
  --session isolated \
  --message "Read the job-tailor skill in the job-search workspace. Run tailor.py --list, handle at most 3 work items, open one PR per posting against $JOBSEARCH_GH_REPO. Report what you opened and nothing else."
```

`--session isolated` keeps each run's context clean and stops a long-lived
session from growing without bound. Use `--session main` only when you want the
run to appear in your main chat thread.

Flags verified against `docs/automation/cron-jobs/payloads.md` (payload table,
agent-turn options) and `docs/automation/cron-jobs/schedules.md` (schedule
kinds, `--cron`, `--tz`).

### What OpenClaw adds that matches this pipeline

- Tool restriction per job: `--tools exec,read` means a tailoring run cannot
  reach the network or a browser unless you allow it.
- Per-job model and fallbacks: `--model`, `--fallbacks`.
- Run history: `openclaw automations runs <job-id>`.

### What still does not automate

The merge. OpenClaw opens the PR, you read the diff in GitHub, and you merge it.
`automerge.py` can report that a structured review covers the current PR head
and provenance, but a model verdict cannot merge or enqueue it. Your GitHub
merge remains the enqueue gate; narrative approval stays owner-only at drain.

---

## Track 3: Hermes Agent

Same five lanes, same split, different scheduler.

### Install

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes model          # pick a model, 64K context minimum
```

The upstream repo is `NousResearch/hermes-agent` (not `NousResearch/hermes`,
which does not exist). After install, `hermes setup` is the onboarding step and
`hermes model` picks the model. 64K context minimum is a Hermes requirement, not
this project's.

### Skills

```bash
mkdir -p ~/.hermes/skills
cp -r ./skills/* ~/.hermes/skills/
```

### Scheduling

Hermes cron takes `--skill`, `--timezone`, `--workdir`, and `--no-agent` for
script-only jobs:

```bash
# mechanical, no model call
hermes cron create "0 8 * * *" \
  --timezone Asia/Singapore \
  --workdir ~/job-search/scripts \
  --no-agent --script ./run.sh --limit 20

# judgement, with the lane skill loaded
hermes cron create "0 9 * * *" \
  --timezone Asia/Singapore \
  --workdir ~/job-search/scripts \
  --skill job-tailor \
  --prompt "Handle at most 3 work items. One PR per posting."
```

`[SILENT]` in a skill's output suppresses the notification when there is nothing
to report, which stops a quiet day from paging you.

### What is verified for Hermes

Checked against the live Hermes docs on 2026-11-05, not assumed:

1. The install one-liner: `https://hermes-agent.nousresearch.com/install.sh`
   returns HTTP 200. There is no `NousResearch/hermes` repo; the project is
   `NousResearch/hermes-agent`.
2. `--skill`, `--timezone`, `--workdir`, `--no-agent`, and `--script` are all
   documented `hermes cron create` flags.
3. `[SILENT]` in an agent response suppresses delivery and still saves output to
   `~/.hermes/cron/output/`.

Not verified: a full lane run on a machine with Hermes installed. The lane logic
is identical to Track 2, so a wrong flag is a rename, not a redesign.

---

## Check it works

Create an isolated synthetic resume repo outside the checkout. `demo/` mirrors
the layout the scripts read:

```bash
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
```

Then run these. Each is independent and cheap:

```bash
cd scripts
python3 test_queue.py
python3 test_queue_eligibility.py
python3 test_queue_recovery.py
python3 test_tailor.py
python3 test_cv_checks.py
python3 test_tailor_preflight.py
python3 test_stages.py
python3 test_dashboard.py
python3 test_config.py
python3 queue.py list
python3 stages.py list
python3 tailor.py --list --force
```

The four required queue, tailor, stages, and dashboard suites print `all
assertions passed`. `test_queue_eligibility.py` and `test_queue_recovery.py`
cover the durable claim, recovery, and tracker-sync boundary. The listing
commands read only the synthetic external repo or runtime paths.

### Live repository access

Before running a live lane, set `JOBSEARCH_GH_REPO`, `JOBSEARCH_REPO_URL`, and
`JOBSEARCH_REPO` to a repository you control, then authenticate `gh`. Without
that setup, PR lookup cannot resolve the placeholder account:

```
GraphQL: Could not resolve to a Repository with the name 'your-user/your-resume-repo'.
```

That placeholder is for offline examples only. In live use, set a real private
repository. A temporary `gh` failure is reported by the relevant lane; do not
interpret missing PR data as proof that no PR exists.

`test_dashboard.py` includes an assertion that no profile value reaches the
rendered HTML. If you add a field to `profile.yaml`, that test starts guarding it
automatically. Do not weaken it.

## What a stranger still has to supply

1. Their own resume repo, with a `data/postings.json` and `applications.yaml`.
2. `gh auth login`, plus write access to that repo.
3. Their optional board list in `scripts/companies.yaml`, or just InternSG.
4. Their own `data/profile.yaml` answers. Start from
   `demo/repo/data/profile.yaml` and replace every value.
5. Their own `rubric.yaml` scoring weights, if the defaults do not match what
   they care about. Eligibility facts come from `data/profile.yaml`.
6. A model, for Tracks 2 and 3.

None of these are optional, and the pipeline is honest about failing when they
are missing rather than guessing.
