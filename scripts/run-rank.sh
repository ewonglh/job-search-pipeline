#!/bin/bash
# Runner for the fit-ranking lane (cron / scheduler friendly).
#
# This lane reads open PRs through gh, so gh must be authenticated once:
#   gh auth login
# Nothing patches credentials here: gh reads the stored identity as-is.
#
# Environment:
#   JOBSEARCH_GH_REPO      your resume repo, "user/repo"
#   JOBSEARCH_REPO         local clone of the resume repo
#   JOBSEARCH_CONTACT_URL  URL put in the User-Agent
#
# Usage: run-rank.sh [rank.py args]
#   run-rank.sh --dry-run
#   run-rank.sh --json
#   run-rank.sh --limit 10 --top 5

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

exec python3 rank.py "$@"
