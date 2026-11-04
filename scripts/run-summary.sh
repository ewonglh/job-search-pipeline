#!/bin/bash
# Runner for the nightly digest lane (cron / scheduler friendly).
#
# run-summary.sh runs gate_summary.py, which decides whether the digest is
# worth delivering before paying for a summary pass. gh must be authenticated.
#
# Environment:
#   JOBSEARCH_GH_REPO  your resume repo, "user/repo"
#   JOBSEARCH_REPO     local clone of the resume repo
#
# Usage: run-summary.sh [gate_summary.py args]
#   run-summary.sh --dry-run
#   run-summary.sh --json
#   run-summary.sh --days 10

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

export JOBSEARCH_GH_REPO="${JOBSEARCH_GH_REPO:-example-user/resume}"
export JOBSEARCH_REPO="${JOBSEARCH_REPO:-$HERE/repo}"

exec python3 gate_summary.py "$@"
