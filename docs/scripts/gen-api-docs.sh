#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCS_DIR="$SCRIPT_DIR/.."
REPO_DIR="$DOCS_DIR/.."

echo "--- Generating API docs ---"

# ── Python API docs (optional — requires sphinx + sphinx-markdown-builder) ──
if python3 -c "import sphinx; import sphinx_markdown_builder" 2>/dev/null; then
  PYTHON_SRC="$REPO_DIR/python/src/bursar"
  PYTHON_OUT="$DOCS_DIR/docs/python-api/reference"
  mkdir -p "$PYTHON_OUT" /tmp/bursar-sphinx /tmp/bursar-sphinx-out

  echo "[python] Running sphinx-apidoc..."
  python3 -m sphinx.ext.apidoc --separate --force -o /tmp/bursar-sphinx "$PYTHON_SRC"

  cat > /tmp/bursar-sphinx/index.rst <<'RST'
bursar API Reference
-------------------

.. toctree::
   :maxdepth: 2

   modules
RST

  echo "[python] Building markdown..."
  sphinx-build -b markdown -c "$SCRIPT_DIR" /tmp/bursar-sphinx /tmp/bursar-sphinx-out

  cp -r /tmp/bursar-sphinx-out/*.md "$PYTHON_OUT/" 2>/dev/null
  echo "[python] Wrote $(ls "$PYTHON_OUT"/*.md 2>/dev/null | wc -l) files"
  rm -rf /tmp/bursar-sphinx /tmp/bursar-sphinx-out
else
  echo "[python] Skipped — sphinx/sphinx_markdown_builder not installed"
fi

echo "--- API docs generation complete ---"
