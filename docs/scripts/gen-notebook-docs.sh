#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCS_DIR="$SCRIPT_DIR/.."
REPO_DIR="$DOCS_DIR/.."
NOTEBOOKS_DIR="$REPO_DIR/samples/python/notebooks"
OUT_DIR="$DOCS_DIR/docs/notebooks"

echo "--- Converting notebooks to Docusaurus MDX ---"

mkdir -p "$OUT_DIR"

for nb in "$NOTEBOOKS_DIR"/[0-9]*.ipynb; do
  name="$(basename "$nb" .ipynb)"
  # Extract title: remove leading number + underscore, replace underscores with spaces, capitalize words
  stem="$(echo "$name" | sed -E 's/^0?[0-9]+_//; s/_/ /g')"
  title="$(python3 -c "import sys; print(sys.argv[1].title())" "$stem")"
  out="$OUT_DIR/$name.mdx"

  # Convert to markdown using nbconvert (try uv-run first, then plain python3)
  PYTHON_DIR="$REPO_DIR/python"
  if command -v uv &>/dev/null; then
    (cd "$PYTHON_DIR" && uv run python -m jupyter nbconvert --to markdown "$nb" --stdout) > "${out}.tmp"
  elif python3 -m jupyter nbconvert --version &>/dev/null; then
    python3 -m jupyter nbconvert --to markdown "$nb" --stdout > "${out}.tmp"
  else
    echo "  WARN: jupyter/nbconvert not installed, skipping $name" >&2
    continue
  fi

  # Inject Docusaurus frontmatter
  { echo "---"
    echo "title: $title"
    echo "sidebar_position: $(echo "$name" | sed -E 's/^0?([0-9]+).*/\1/')"
    echo "---"
    echo ""
    cat "${out}.tmp"
  } > "$out"

  rm -f "${out}.tmp"
  echo "  $name → notebooks/$name.mdx"
done

echo "--- Done: $(ls "$OUT_DIR"/*.mdx 2>/dev/null | wc -l) notebooks converted ---"
