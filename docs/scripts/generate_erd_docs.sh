#!/usr/bin/env bash
set -euo pipefail
BURSAR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${BURSAR_ERD_DATABASE_URL:?Set BURSAR_ERD_DATABASE_URL to a migrated disposable database}"
cd "$BURSAR_ROOT/python"
uv run --extra postgres python "$BURSAR_ROOT/docs/scripts/generate_erd.py"
