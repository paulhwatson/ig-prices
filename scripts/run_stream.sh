#!/bin/bash
# Wrapper for the IG candle collector under launchd, which starts with almost
# no environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec "$REPO_ROOT/.venv/bin/python" -m prices.main stream "$@"
