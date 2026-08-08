#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCS_DIR="$SCRIPT_DIR/.."
TEMP_DIR="$(mktemp -d)"

trap 'rm -rf "$TEMP_DIR"' EXIT

echo "--- Generating API docs ---"

if ! python3 -c "import sphinx; import sphinx_markdown_builder" 2>/dev/null; then
  echo "[python] Missing Sphinx dependencies. Install sphinx and sphinx-markdown-builder before building the documentation." >&2
  exit 1
fi

PYTHON_OUT="$DOCS_DIR/docs/python-api/reference"
SPHINX_SOURCE_TEMPLATE="$DOCS_DIR/sphinx"
SPHINX_SRC="$TEMP_DIR/sphinx-source"
SPHINX_OUT="$TEMP_DIR/sphinx-output"

mkdir -p "$PYTHON_OUT" "$SPHINX_SRC" "$SPHINX_OUT"
cp -R "$SPHINX_SOURCE_TEMPLATE"/. "$SPHINX_SRC"/
find "$PYTHON_OUT" -type f -name '*.md' -delete
find "$PYTHON_OUT" -depth -type d -empty -delete

echo "[python] Building the public API reference..."
python3 -m sphinx -q -W --keep-going -b markdown -c "$SCRIPT_DIR" "$SPHINX_SRC" "$SPHINX_OUT"

cp -R "$SPHINX_OUT"/. "$PYTHON_OUT/"
echo "[python] Wrote $(find "$PYTHON_OUT" -type f -name '*.md' | wc -l | tr -d ' ') files"

echo "--- API docs generation complete ---"
