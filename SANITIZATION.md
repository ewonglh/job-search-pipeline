# Sanitization boundary

What may leave this machine, and what may not. Read this before copying any file
from the working pipeline into a repo, a gist, a screenshot, or a chat.

The real pipeline is personal. It knows who you are, which companies rejected you,
and what recruiters wrote to you. A public guide must ship the mechanism and none
of the person.

## The three zones

| Zone | Files | Rule |
| --- | --- | --- |
| **Local only, never committed** | `profile.local.yaml` | 0600, outside git, read only when a form demands it |
| **Private repo only** | `repo/applications.yaml`, `repo/data/postings.json`, `queue.yaml`, `skipped.yaml`, `deadlines.yaml`, `inbox/`, `outbox/`, `cover-letters/` | Real employment history and real correspondence |
| **Publishable** | the `.py` lane scripts, `test_*.py`, `run-*.sh`, `rubric.yaml`, `companies.yaml`, `gates.yaml` | Mechanism only, no person |

## What is in `profile.local.yaml`

Verified content, 2026-11-04: `date_of_birth`, `gender`, `nationality`, `race`,
`address`. File mode 0600.

- **NRIC is deliberately absent.** It is never stored. Either the owner types it
  into the form directly, or `run-nric-fill.sh` injects it from the secret store
  over CDP and prints only a status line (`README.md:1147-1160`,
  `README.md:797-800`).
- Nothing from this file may appear in a repo, a log, a chat message, or a
  screenshot. The employer's own form is the only destination.
- A public repo ships no equivalent file at all. The demo profile carries synthetic
  values and says so at the top.

## What is in `repo/data/profile.yaml`

Non-sensitive form answers only, by design: name, email, phone, LinkedIn, GitHub,
university, degree, expected graduation, GPA, work authorisation, standing answers
to recurring questions.

For a public repo this file is **still personal**: a real name plus a real email
plus a real phone number is doxxing data even without a passport number. The demo
version must be fully synthetic.

## The tracker is employment history

`repo/applications.yaml` holds 111 companies with real roles, real dates, and real
outcomes: who rejected, who interviewed, who offered. That is a personal record
even though no identifier appears in it.

Published rule, from the pipeline's own docs (`README.md:604-610`): nothing is
submitted without being documented here. A rebuild must keep that rule, and must
keep this file out of any public artifact.

## Correspondence

`inbox/` and `outbox/` hold real recruiter names, real email addresses, and real
message content. `actions.py` reads `outbox/*.md` to build the draft list
(`actions.py:12`), so the directory is load-bearing, not incidental.

A public repo ships an empty `outbox/` with a `.gitkeep` and one synthetic example.

## The fixtures are NOT clean in the source repo

This is the trap, and it applies to the repo you are copying *from*. This repo's
`scripts/fixtures/` is already regenerated with a fictional persona (Alex Morgan,
`demo-user/resume`), and all four offline suites pass against it. The warning
below is what to check if you are importing from a live pipeline instead.

`fixtures/` exists for the offline tests, so it is tempting to copy wholesale from
a live tree. Do not. Verified 2026-11-04 against the original:

| File | What it leaks |
| --- | --- |
| `fixtures/<resume-variant>/resume.tex` (line 11-16) | Full name in large type, real phone, real email, and both real profile handles in the heading block |
| `fixtures/gmail/*.eml` | The owner's real email in `To:`, and the owner's first name in salutations |
| `fixtures/queue/merged_prs.json` | Real private-repo PR URLs and the owner's GitHub login |

Seven `.eml` fixtures and one `.tex` fixture carry identity data. Any public copy
must regenerate them with a fictional persona. This file deliberately does not
reproduce the actual strings: naming them here would leak exactly what it forbids.
Run the greps in the next section against your own tree to confirm the real values
are absent.

## Per-lane check before publishing

1. Grep your own tree for your name, email, phone, and GitHub handle. It must
   return nothing outside a comment that says what to remove.
2. `grep -rn "linkedin.com/in\|github.com/" .` returns only the guide's own links.
3. No file under `fixtures/` or `demo/` came from the real pipeline unmodified.
4. `git log --stat` on the new repo shows no import of the real tree.
5. `python3 test_*.py` passes with the synthetic fixtures, which is the actual
   proof the demo is complete: if the tests need real data, the demo is not done.

## What the guide itself may say

Fine to publish: the lane architecture, the state files and their schemas, the
approval gates, the sanitization rules, the failure modes (duplicate PRs,
truncated mail pagination, stale hand-written deadline lists), and the reasoning
behind each.

Not fine: real company names paired with real outcomes, real recruiter names,
real dates of real applications, real PR numbers on a real private repo, and any
screenshot of a live brief.
