#!/bin/bash
# Wrapper for the weekly backfill: launchd starts with almost no environment,
# so everything the run needs is spelled out here rather than inherited.
#
# backfill, not update: the live stream keeps everything current for free, so
# the only thing a scheduled fetch has left to do is deepen the past - and
# there is exactly one weekly allowance of that to spend.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec "$REPO_ROOT/.venv/bin/python" -m prices.main backfill "$@"
