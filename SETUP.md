# Setup

Three tracks, same pipeline. Pick the one that matches what you already run.

Common prerequisites for all three:

- Python 3.9 or newer, stdlib only. No pip install.
- `git`
- `gh` authenticated once: `gh auth login`
- A resume repo of your own. The scripts read `data/postings.json` and
  `applications.yaml` from a clone, and open PRs against it.

Set these before anything else:

```bash
export JOBSEARCH_GH_REPO="your-user/your-resume-repo"
export JOBSEARCH_REPO_URL="https://github.com/your-user/your-resume-repo.git"
export JOBSEARCH_REPO="$HOME/job-search/repo"
```

Put them in your shell rc file so every scheduled job inherits them. A cron job
that cannot see these variables will clone the wrong repo or fail.

---

## Track 1: plain cron, no agent

The lowest common denominator. Costs nothing, needs no model, and covers the two
lanes that run unattended safely: discovery and ranking.

### What it can run

| Lane | Runs | Why |
|---|---|---|
| Discover | yes | Pure HTTP plus git. No judgement needed |
| Rank | yes | Deterministic scoring against `rubric.yaml` |
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
0 8 * * *  cd $HOME/job-search/scripts && JOBSEARCH_GH_REPO=your-user/your-resume-repo ./run.sh --limit 20 >> $HOME/job-search/discover.log 2>&1

# ranking, after discovery
30 8 * * * cd $HOME/job-search/scripts && JOBSEARCH_GH_REPO=your-user/your-resume-repo ./run-rank.sh --top 10 >> $HOME/job-search/rank.log 2>&1

# queue enqueue, hourly, cheap and silent when nothing merged
15 * * * * cd $HOME/job-search/scripts && JOBSEARCH_GH_REPO=your-user/your-resume-repo ./run-queue.sh >> $HOME/job-search/queue.log 2>&1
```

Cron does not inherit your shell environment. The `JOBSEARCH_GH_REPO=...` prefix
on each line is not redundant, it is the thing that stops a job writing to the
wrong repo.

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

The merge. OpenClaw opens the PR, you read the diff in GitHub, you merge. That is
the gate, and no scheduler changes it.

---

## Track 3: Hermes Agent

Same five lanes, same split, different scheduler.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes/main/install.sh | bash
hermes model          # pick a model, 64K context minimum
```

The install is a one-liner and `hermes model` is the onboarding step. Verify both
against the Hermes docs before relying on them, and see the unverified list
below.

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

### What is unverified for Hermes

Three claims in this track come from Hermes documentation rather than a run on
this machine. Treat them as close but not certain:

1. The exact install one-liner URL.
2. Whether `--workdir` is the correct flag name for Hermes cron.
3. Whether `[SILENT]` is honoured for every skill output or only some.

The lane logic is identical to Track 2, so if a flag is wrong the fix is a flag
name, not a redesign.

---

## Check it works

First load the demo data into the shape the scripts expect. `demo/` mirrors the
layout of a resume repo, so one copy is enough:

```bash
cd scripts
mkdir -p repo/data
cp ../demo/repo/data/postings.json  repo/data/postings.json
cp ../demo/repo/data/profile.yaml   repo/data/profile.yaml
cp ../demo/repo/applications.yaml   repo/applications.yaml
cp ../demo/queue.yaml ../demo/skipped.yaml ../demo/deadlines.yaml .
```

Then run these. Each is independent and cheap:

```bash
python3 test_queue.py            # queue logic, offline
python3 test_tailor.py           # tailoring selection, offline
python3 test_stages.py           # stage machine, offline
python3 test_dashboard.py        # board rendering and the PII assertion
python3 dashboard.py --json      # prints the computed board, no files written
python3 queue.py list            # reads the demo queue
python3 stages.py list           # reads the demo tracker
python3 tailor.py --list         # reports selection counts
```

Expected: the four `test_*.py` suites print `all assertions passed`, and the
three listing commands produce rows.

### The one failure you will see with demo data

`tailor.py`, `rank.py`, `dashboard.py` and `queue.py` call `gh` to check for open
pull requests. With `JOBSEARCH_GH_REPO=example-user/resume` that repo does not
exist, so you get:

```
GraphQL: Could not resolve to a Repository with the name 'example-user/resume'.
```

That is correct behaviour, not a bug. Point `JOBSEARCH_GH_REPO` at a repo you own
and the warning goes away. The scripts degrade rather than crash: `tailor.py`
says `WARNING in-flight check unavailable`, and `dashboard.py` says `Found may
over-count`. Read those warnings as "the PR check is blind", not "no PRs exist".

`test_dashboard.py` includes an assertion that no profile value reaches the
rendered HTML. If you add a field to `profile.yaml`, that test starts guarding it
automatically. Do not weaken it.

## What a stranger still has to supply

1. Their own resume repo, with a `data/postings.json` and `applications.yaml`.
2. `gh auth login`, plus write access to that repo.
3. Their board list in `companies.yaml`, or just InternSG alone.
4. Their own `profile.yaml` answers. Start from `demo/profile.yaml` and replace
   every value.
5. Their own `rubric.yaml` weights, if the defaults do not match what they care
   about.
6. A model, for Tracks 2 and 3.

None of these are optional, and the pipeline is honest about failing when they
are missing rather than guessing.
