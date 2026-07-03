#!/usr/bin/env python3
"""Convert Jupyter notebooks to Docusaurus MDX files with frontmatter."""
import re
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
NB_DIR = REPO_DIR / "samples" / "python" / "notebooks"
OUT_DIR = REPO_DIR / "docs" / "docs" / "notebooks"

OUT_DIR.mkdir(parents=True, exist_ok=True)

for nb in sorted(NB_DIR.glob("[0-9]*.ipynb")):
    name = nb.stem
    stem = re.sub(r"^0?\d+_", "", name).replace("_", " ")
    title = stem.title()
    pos = name.split("_")[0].lstrip("0")

    md = subprocess.run(
        [sys.executable, "-m", "jupyter", "nbconvert", "--to", "markdown", str(nb), "--stdout"],
        capture_output=True, text=True, check=True,
    ).stdout

    out = OUT_DIR / f"{name}.mdx"
    out.write_text(f"---\ntitle: {title}\nsidebar_position: {pos}\n---\n\n{md}")
    print(f"  {name} → notebooks/{name}.mdx")

print(f"--- Done: {len(list(OUT_DIR.glob('*.mdx')))} notebooks converted ---")
