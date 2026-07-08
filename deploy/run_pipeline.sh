#!/usr/bin/env bash
# Legacy wrapper: forwards to deploy/run_daily.sh.
# Kept so old docs / muscle memory still work.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
exec /bin/bash deploy/run_daily.sh