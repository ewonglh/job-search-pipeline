#!/bin/bash
# Runner for the NRIC / national-ID sentinel-fill helper.
#
# The value comes only from the environment variable NRIC_SG. It is never
# passed on the command line, so it cannot land in shell history or a process
# list. The helper prints exactly one status line (nric_fill: ...).
#
# Usage:
#   run-nric-fill.sh --check     # present | missing
#   run-nric-fill.sh             # type into the focused field
#
# Optional env overrides (discovery / targeting only, never the value):
#   NRIC_FILL_CDP_HTTP, NRIC_FILL_CDP_WS, NRIC_FILL_TARGET

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

exec python3 nric_fill.py "$@"
