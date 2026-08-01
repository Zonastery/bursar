#!/usr/bin/env python3
"""Convert Jupyter notebooks to Docusaurus MDX files with frontmatter."""
import re
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
NB_DIR = REPO_DIR / "samples" / "python" / "notebooks"
OUT_DIR = REPO_DIR / "docs" / "docs" / "notebooks"

_ACRONYMS = {"CLI", "API"}
_LOWER_WORDS = {"and", "of", "to"}


def notebook_title(name: str) -> str:
    """Title-case a notebook stem: capitalize each word, lowercase
    ``and``/``of``/``to`` mid-title, and keep known acronyms uppercase
    (``00_why_bursar_and_setup`` → ``Why Bursar and Setup``,
    ``13_cli_and_deployment`` → ``CLI and Deployment``)."""
    words = re.sub(r"^0?\d+_", "", name).replace("_", " ").split()
    out = []
    for i, word in enumerate(words):
        if word.upper() in _ACRONYMS:
            out.append(word.upper())
        elif i > 0 and word.lower() in _LOWER_WORDS:
            out.append(word.lower())
        else:
            out.append(word.capitalize())
    return " ".join(out)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    notebooks = sorted(NB_DIR.glob("[0-9]*.ipynb"))
    current_stems = {nb.stem for nb in notebooks}

    # Delete stale generated pages so removed notebooks don't leave orphans.
    for stale in OUT_DIR.glob("*.mdx"):
        if stale.stem not in current_stems:
            stale.unlink()
            print(f"  removed {stale.name} (no matching notebook)")

    for nb in notebooks:
        name = nb.stem
        title = notebook_title(name)
        pos = int(name.split("_")[0]) + 1

        md = subprocess.run(
            [sys.executable, "-m", "jupyter", "nbconvert", "--to", "markdown", str(nb), "--stdout"],
            capture_output=True, text=True, check=True,
        ).stdout

        out = OUT_DIR / f"{name}.mdx"
        out.write_text(f"---\ntitle: {title}\nsidebar_position: {pos}\n---\n\n{md}")
        print(f"  {name} → notebooks/{name}.mdx")

    print(f"--- Done: {len(notebooks)} notebooks converted ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
