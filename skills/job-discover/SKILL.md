---
name: job-discover
description: Discover new internship postings from public job boards and merge the new ones into the posting store without deleting or rewriting existing entries. Use when a scheduled discovery lane runs, when the user asks to find new internship postings, or when a browser lane has already captured LinkedIn results and needs them merged.
---

# Discover lane

## What it does

Fetches internship postings from InternSG and any configured company career
pages, keeps the ones that look like Singapore SWE or tech internships, and
merges only the **new** ones into `data/postings.json` inside a clone of the
tracker repo. Existing entries are never deleted or rewritten.

Dedup key is a normalised `company|role|url`. A posting whose normalised key is
already in the store is skipped silently.

This lane never drives a browser. The LinkedIn path feeds it through
`--import-json`, either a file path or `-` for stdin.

## Inputs

- `companies.yaml` and `companies-registry.yaml` in the project root: which boards
  and career pages to probe.
- Optionally a LinkedIn capture file via `--import-json`.
- A local clone of the tracker repo, with push access.

## Outputs

- One digest line per NEW posting on stdout: `<company> · <role> · <link>`.
- Everything else (diagnostics, git output) goes to stderr, so stdout stays
  machine-readable.
- A commit on `main` of the tracker repo when `--dry-run` is absent.

## Procedure

1. Run `python3 discover.py --dry-run` first when the config changed. It prints
   what it would add and touches nothing.
2. Real run: `python3 discover.py`. It pulls, merges, commits and pushes to
   `main`.
3. Report the digest lines verbatim. If there are none, say so in one line.

## Idempotency

Re-running is safe. The dedup key is normalised, and existing entries are never
rewritten, so a second run in the same hour adds nothing.

## Stop conditions

- No push access, or a non-fast-forward pull: stop and report, do not force.
- A board returns HTML that does not parse to the expected shape: report the board
  name and the URL, do not guess at postings.
- `--dry-run` was requested: never escalate to a real run on your own.

## Silence

If nothing new was found, reply with only the configured silent token for this
system (in Hermes: `[SILENT]`) so the cron tick delivers nothing.
