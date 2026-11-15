#!/bin/bash
# Runner for the discovery lane (cron / scheduler friendly).
#
# Configuration is validated by discover.py. Set the resume repository and
# clone URL explicitly for scheduled runs; defaults are neutral placeholders.
#
# Usage: run.sh [discover.py args]
#   run.sh --dry-run --limit 20
#   run.sh --import-json /tmp/linkedin.json

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

exec python3 discover.py "$@"
