#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_PYTHON="$SCRIPT_DIR/../../python/.venv/bin/python"

if [[ -n "${BURSAR_DOCS_PYTHON:-}" ]]; then
  DOCS_PYTHON="$BURSAR_DOCS_PYTHON"
elif [[ -x "$LOCAL_PYTHON" ]]; then
  DOCS_PYTHON="$LOCAL_PYTHON"
else
  DOCS_PYTHON="python3"
fi

exec "$DOCS_PYTHON" "$@"
