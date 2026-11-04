# Job-search pipeline

A split pipeline for internship applications: deterministic scripts do the
mechanical half, an agent does the judgement half, and a git merge is the
approval gate for every outbound action.

Harness-agnostic. The scripts are plain Python 3 stdlib. Any scheduler can run
them: cron, systemd, OpenClaw, Hermes Agent, Claude Code, or a human typing
commands. See [SETUP.md](SETUP.md) for three worked tracks.

**Built for the Singapore internship market, and wired for it.** Discovery
targets InternSG and company career pages, the ranker scores a Singapore base
highest and treats overseas postings as a 0.3 penalty, and the demo profile is a
Singapore student. Running it elsewhere works, but you will be editing
`scripts/rubric.yaml`, the `InternSG` source list in `scripts/discover.py`, and
the `NRIC_SG` handling. That is a port, not a config change, so look at it before
you clone.

## The core rule

Nothing leaves your machine without a human-approved merge.

1. A script writes a work item, or a script opens a pull request.
2. You read the diff.
3. You merge.
4. Only then does a queue item exist, and only then does anything get submitted.

The scripts never submit an application on their own. The agent never merges its
own work. The merge is the consent event.

## Five lanes

| Lane | Script | Runs without an agent | What it does |
|---|---|---|---|
| Discover | `discover.py` | yes | Fetch postings from boards, keep new ones, commit to your resume repo |
| Rank | `rank.py` | yes | Deterministic zero-token scoring against `rubric.yaml`, vetoes hostile postings |
| Tailor | `tailor.py` | no, emits work items | Select untracked postings, fetch each JD, resolve the real apply URL |
| Drain | `queue.py` | no | Turn merged `app/*` PRs into a submit queue, oldest first |
| Track | `stages.py` `summary.py` `dashboard.py` | mostly | Stage machine, digests, local board |

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

## What this does not do

- Does not auto-submit. A merged PR puts an item in a queue that a human or an
  agent drains, and the drain still stops at captchas and unseen questions.
- Does not bypass bot detection, captchas, or rate limits.
- Does not scrape at scale. Discovery is a handful of boards you name.
- Is not market-neutral. See the note at the top: it is built for Singapore.
- Does not store passports, national IDs, or birthdates. By design, see
  `SANITIZATION.md`.
- Does not promise a job. It reduces the cost of applying carefully.

## Quick start

```bash
git clone https://github.com/ewonglh/job-search-pipeline.git
cd job-search-pipeline
export JOBSEARCH_GH_REPO="your-user/your-resume-repo"
export JOBSEARCH_REPO_URL="https://github.com/your-user/your-resume-repo.git"

mkdir -p scripts/repo/data
cp demo/repo/data/postings.json scripts/repo/data/postings.json
cp demo/repo/data/profile.yaml  scripts/repo/data/profile.yaml
cp demo/repo/applications.yaml  scripts/repo/applications.yaml
cp demo/queue.yaml demo/skipped.yaml demo/deadlines.yaml scripts/

cd scripts && python3 test_queue.py && python3 test_tailor.py
```

`demo/` mirrors the layout of a real resume repo, so one copy is enough.
The data is fictional. It exists so the tests run and so you can see the shape of
every file before you fill it with anything real.

## Configuration

Every owner-specific value is an environment variable. Nothing is hardcoded to
the original author's accounts.

| Variable | Meaning | Default |
|---|---|---|
| `JOBSEARCH_GH_REPO` | Your resume repo, `user/repo` | `example-user/resume` |
| `JOBSEARCH_REPO_URL` | Clone URL for it | `https://github.com/$JOBSEARCH_GH_REPO.git` |
| `JOBSEARCH_REPO` | Local clone path | `./repo` |
| `JOBSEARCH_CONTACT_URL` | URL placed in the HTTP User-Agent | `https://github.com/example-user` |
| `JOBSEARCH_QUEUE` | Queue store | `./queue.yaml` |
| `JOBSEARCH_SKIPPED` | Owner-skip store | `./skipped.yaml` |
| `JOBSEARCH_DEADLINES` | Deadline store | `./deadlines.yaml` |
| `JOBSEARCH_OUTBOX` | Draft outbox | `./outbox` |

`gh` must be authenticated once (`gh auth login`). Discovery, rank, and queue
read your repo through it.

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
