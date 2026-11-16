---
name: job-tailor
description: Turn a scored posting into a tailored resume variant and open it as a pull request for review. Use when tailoring is required for a shortlisted posting, when the user asks to tailor a resume for a role, or when a scheduled tailoring lane runs. The merge of the PR is the approval, never an automatic submit.
---

# Tailor lane

## Split of work

The **mechanical** half is code: select postings, fetch the job description,
resolve the company's own apply URL, write one JSON work item per posting under
`work/<slug>.json`. The **judgement** half is this agent turn: choose and rewrite
resume evidence for the role, then open a PR.

Never do the mechanical half by hand. Never do the judgement half with a regex.

## Inputs

- `work/<slug>.json`: the work item, with `jd_text`, company, role, link and the
  resolved apply URL.
- The resume master file in the tracker repo.
- The tailoring doctrine and template in the tracker repo.

## Outputs

- A branch named `app/<company-slug>-<role-slug>`.
- A changed `variants/<slug>/resume.tex` plus the rendered PDF.
- One pull request against `main`, body stating the company, role, posting link,
  and which evidence was chosen and why.

## Procedure

1. Read the work item. Require `status: ready`. If `jd_text` is empty or the guard verdict is `needs_jd`,
   stop and report; do not tailor from a guessed description.
   Stop for `eligibility.status: ineligible` or `unknown`, a `posting_closed`
   hold, or any missing/unavailable input. `--force` skips duplicate selection
   only; it never bypasses these holds.
2. Select evidence from the master resume that genuinely matches the posting.
   Rewriting is compression and reordering, never invention.
3. Build the variant. Run the deterministic structural checks from `scripts/`:

   ```bash
   python3 cv_checks.py --master "$JOBSEARCH_REPO/master/resume.tex" \
     --resume "$JOBSEARCH_REPO/variants/<slug>/resume.tex" --json
   ```

   A finding requests review; an empty finding list only means these structural
   checks passed. They do not prove semantic truth. Build/render the PDF using
   the configured resume toolchain and check its page count.
4. Fetch the posting again immediately before reuse or PR review. Save its
   current JD text to a separate private file; do not copy it from an old work
   item. If retrieval fails, stop for review. After the artifact is committed
   on its branch, run the exact-input preflight
   from `scripts/`:

   ```bash
   python3 tailor_preflight.py \
     --work-item "$JOBSEARCH_WORK/<slug>.json" \
     --master "$JOBSEARCH_REPO/master/resume.tex" \
     --profile "$JOBSEARCH_REPO/data/profile.yaml" \
     --artifact "$JOBSEARCH_REPO/variants/<slug>/resume.tex" \
     --current-jd "$JOBSEARCH_WORK/<slug>.current-jd.txt" \
     --head "$(git -C "$JOBSEARCH_REPO" rev-parse HEAD)" \
     --pdf "$JOBSEARCH_REPO/variants/<slug>/resume.pdf"
   ```

   The report compares exact master, profile, fresh guarded JD, role/title and
   artifact hashes. The CLI resolves the artifact checkout's actual Git HEAD
   and verifies that the file matches its committed bytes. Without a layout checker or a matching prior owner-accepted
   record it reports `review_required`; unavailable inputs and stale evidence
   are never clean results. `--accepted-record PATH` may check an existing
   `owner_accepted_preflight` record (schema version 1, `accepted`,
   `accepted_by`, `accepted_at`, `input_hashes`, `artifact_sha256`, and `head`).
   The checker does not create or promote that record, and its result cannot
   authorize a merge or queue enqueue.
5. Open the PR. State the posting link in the body. Do not merge it yourself.
6. Report the PR number, the branch, and one line on what changed.

## Approval gates

- **A merged tailoring PR is the CV approval.** Merging enqueues the application
  and records the approval automatically. No second go-ahead is asked for that CV.
- **Nothing is submitted from an unmerged branch.** No PR merge, no application.
- **Narrative content always waits for the owner.** Personal statements and cover
  letters are drafted and presented, never sent on autopilot.
- Never force-push a `app/*` branch that already has a PR. Push normal commits to
  the same branch.

## Idempotency

Before tailoring, check whether this posting already has a PR in **any** state.
A merged or closed PR for the same posting link means the work is done: skip it.
Checking only open PRs is how the same posting got tailored twice in the original
system.

## Stop conditions

- The posting link already appears in an open, merged, or closed PR: skip.
- The job description cannot be fetched: mark `needs_jd` and stop.
- The master resume is missing or the render fails: stop and report the error.
- The role fails the SWE filter: skip it, do not tailor outside scope.
