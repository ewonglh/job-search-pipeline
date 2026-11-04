#!/bin/bash
# Runner for the application-queue lane (cron / scheduler friendly).
#
# With no arguments (or a leading flag) it defaults to the ``enqueue`` subcommand.
# This lane shells out to gh, which must be authenticated once: gh auth login.
#
# Environment:
#   JOBSEARCH_GH_REPO  your resume repo, "user/repo"
#   JOBSEARCH_QUEUE    queue store path (default ./queue.yaml)
#
# Usage: run-queue.sh [queue.py args]
#   run-queue.sh                      # enqueue (silent when nothing merged)
#   run-queue.sh next
#   run-queue.sh list --status queued

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

if [ "$#" -eq 0 ]; then
  set -- enqueue
fi
case "${1:-}" in
  -*) set -- enqueue "$@" ;;
esac

export JOBSEARCH_GH_REPO="${JOBSEARCH_GH_REPO:-example-user/resume}"
export JOBSEARCH_QUEUE="${JOBSEARCH_QUEUE:-$HERE/queue.yaml}"

exec python3 queue.py "$@"
