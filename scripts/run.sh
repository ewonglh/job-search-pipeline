#!/bin/bash
# Runner for the discovery lane (cron / scheduler friendly).
#
# Every value that used to be hardcoded is an environment variable:
#   JOBSEARCH_GH_REPO      your resume repo, "user/repo" (default example-user/resume)
#   JOBSEARCH_REPO_URL     clone URL (default https://github.com/$JOBSEARCH_GH_REPO.git)
#   JOBSEARCH_REPO         local clone of the resume repo (default ./repo)
#   JOBSEARCH_CONTACT_URL  URL put in the User-Agent (default https://github.com/example-user)
#
# Usage: run.sh [discover.py args]
#   run.sh --dry-run --limit 20
#   run.sh --import-json /tmp/linkedin.json

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

export JOBSEARCH_GH_REPO="${JOBSEARCH_GH_REPO:-example-user/resume}"
export JOBSEARCH_REPO_URL="${JOBSEARCH_REPO_URL:-https://github.com/${JOBSEARCH_GH_REPO}.git}"
export JOBSEARCH_REPO="${JOBSEARCH_REPO:-$HERE/repo}"

exec python3 discover.py "$@"
