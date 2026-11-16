# Sanitization boundary

This project separates public code and synthetic examples from private resume
data and application state. Keep private files outside the checkout and inspect
any material before sharing it in a repository, screenshot, or message.

## Data zones

| Zone | Examples | Rule |
| --- | --- | --- |
| Local identity answers | `profile.local.yaml` | Keep outside Git with owner-only permissions. Read only when a form requires an answer. |
| Private resume repository | `applications.yaml`, `data/profile.yaml`, `data/postings.json`, `master/`, `variants/` | Contains personal history and application materials. Use a private repository. |
| Private runtime state | `queue.yaml`, `skipped.yaml`, `deadlines.yaml`, `recruitment_events.json`, `email_ingest_state.json`, `submission-snapshots/`, `work/`, `inbox/`, `outbox/`, `cover-letters/`, state and cache files | Keep outside the checkout. It may contain decisions, correspondence, or application details. |
| Publishable project files | scripts, tests, workflow, neutral configuration, documentation | Include only the mechanism and synthetic examples. |

## Resume and form data

`data/profile.yaml` may contain a name, contact details, education, work
authorization, and standing form answers. Those values identify a person and
belong only in that person's private resume repository. Keep identity documents,
government identifiers, birth dates, and home addresses out of the repo and
runtime logs. A public demo profile must use fictional values and say so.

`applications.yaml` records employment and application history. Treat it as
private even if names and contact details are absent. Keep it out of public
artifacts. Outgoing and incoming correspondence, cover letters, queue notes, and
review evidence may also identify people or reveal private application details.

Submission snapshots retain exact resume PDF and answers JSON bytes under
`JOBSEARCH_SNAPSHOT_ROOT` (default:
`~/.local/state/job-search-pipeline/submission-snapshots`). Keep this directory
outside both the public checkout and private resume repository. On POSIX the
snapshot directories are mode `0700` and files are mode `0400`; on Windows use
the user-local state directory so the files inherit the user's profile access
controls. The queue stores content hashes, status/error fields, and
application/attempt bindings, not the artifact bytes or snapshot directory
path. Hashes establish stored-byte identity, not employer receipt.

## Before publishing

1. Search the working tree for names, contact details, account handles, and
   private repository URLs. Review each match in context.
2. Check that every file under `demo/` and test fixture directories is synthetic.
3. Inspect the staged diff and commit contents before publishing.
4. Run the offline tests using synthetic data. Tests must not require private
   resume files or live state.
5. Do not include screenshots, logs, or test output captured from a live
   application pipeline.

The public guide may describe the lane architecture, schemas, approval gates,
sanitization rules, and general failure modes. Do not publish real application
outcomes, correspondence, dates, private pull request details, or live pipeline
screenshots.
