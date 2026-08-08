"""Render validated Bursar notebooks as Docusaurus tutorial pages."""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import nbformat
from nbconvert import MarkdownExporter

REPO_DIR = Path(__file__).resolve().parents[2]
NOTEBOOK_DIR = REPO_DIR / "samples" / "python" / "notebooks"
OUTPUT_DIR = REPO_DIR / "docs" / "docs" / "notebooks"

REPOSITORY_URL = "https://github.com/zonastery/bursar"
NOTEBOOK_PATTERN = re.compile(r"^(?P<position>\d{2})_(?P<slug>[a-z0-9_]+)$")
DIRECT_IDEMPOTENT_METHODS = {
    "add_credits",
    "deduct",
    "deduct_credits",
    "refund_credits",
}
OPTIONS_IDEMPOTENT_METHODS = {
    "reserve": "ReserveOptions",
    "settle": "SettleOptions",
}

SECTIONS = {
    "foundations": {
        "label": "Foundations",
        "position": 1,
        "title": "Build a Bursar foundation",
        "description": "Configure pricing, evaluate usage, and understand the expression language before persisting account state.",
    },
    "credits-and-controls": {
        "label": "Credits and controls",
        "position": 2,
        "title": "Operate credits and account controls",
        "description": "Work with balances, allowances, quotas, expiry, leases, teams, analytics, and lifecycle events.",
    },
    "billing-and-operations": {
        "label": "Billing and operations",
        "position": 3,
        "title": "Connect billing and operate Bursar",
        "description": "Integrate subscriptions, deploy configuration, evaluate custom stores, and inspect the full schema.",
    },
}


class NotebookError(ValueError):
    """Report a notebook that cannot be published as documentation."""


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


def _validate_idempotency(source: str, path: Path, cell_number: int) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        location = f"line {error.lineno}" if error.lineno else "an unknown line"
        raise NotebookError(
            f"{path.name}: code cell {cell_number} has invalid Python at {location}: "
            f"{error.msg}"
        ) from error

    missing: list[tuple[str, int]] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = _call_name(call)
        if name in DIRECT_IDEMPOTENT_METHODS and not _has_keyword(
            call, "idempotency_key"
        ):
            missing.append((name, call.lineno))
        elif name in OPTIONS_IDEMPOTENT_METHODS:
            option_type = OPTIONS_IDEMPOTENT_METHODS[name]
            options = [
                nested
                for nested in ast.walk(call)
                if isinstance(nested, ast.Call) and _call_name(nested) == option_type
            ]
            if not any(_has_keyword(option, "idempotency_key") for option in options):
                missing.append((name, call.lineno))

    if missing:
        calls = ", ".join(f"{name} (line {line})" for name, line in missing)
        raise NotebookError(
            f"{path.name}: code cell {cell_number} must pass stable idempotency "
            f"keys to replayable calls: {calls}"
        )


def _metadata(notebook: nbformat.NotebookNode, path: Path) -> dict[str, Any]:
    metadata = notebook.metadata.get("bursar_docs")
    if not isinstance(metadata, dict):
        raise NotebookError(f"{path.name}: metadata.bursar_docs is required")

    required = ("title", "sidebar_label", "description", "section")
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise NotebookError(
            f"{path.name}: missing bursar_docs fields: {', '.join(missing)}"
        )
    if metadata["section"] not in SECTIONS:
        choices = ", ".join(SECTIONS)
        raise NotebookError(
            f"{path.name}: unknown section {metadata['section']!r}; use {choices}"
        )
    return metadata


def _validate_notebook(
    notebook: nbformat.NotebookNode, path: Path, metadata: dict[str, Any]
) -> None:
    cell_ids = [str(cell.get("id", "")) for cell in notebook.cells]
    missing_ids = [
        index for index, cell_id in enumerate(cell_ids, start=1) if not cell_id
    ]
    if missing_ids:
        positions = ", ".join(map(str, missing_ids))
        raise NotebookError(f"{path.name}: cells {positions} are missing stable IDs")

    duplicate_ids = sorted(
        cell_id for cell_id, count in Counter(cell_ids).items() if count > 1
    )
    if duplicate_ids:
        duplicates = ", ".join(duplicate_ids)
        raise NotebookError(f"{path.name}: duplicate cell IDs: {duplicates}")

    nbformat.validate(notebook)

    if not notebook.cells or notebook.cells[0].cell_type != "markdown":
        raise NotebookError(f"{path.name}: first cell must be Markdown")

    markdown = "\n".join(
        str(cell.source) for cell in notebook.cells if cell.cell_type == "markdown"
    )
    expected_title = f"# {metadata['title']}"
    if not str(notebook.cells[0].source).startswith(expected_title):
        raise NotebookError(f"{path.name}: first heading must be {expected_title!r}")
    for heading in ("## Learning objectives", "## Prerequisites"):
        if heading not in markdown:
            raise NotebookError(f"{path.name}: missing {heading!r}")

    dirty_cells = [
        index
        for index, cell in enumerate(notebook.cells, start=1)
        if cell.cell_type == "code"
        and (cell.get("execution_count") is not None or cell.get("outputs"))
    ]
    if dirty_cells:
        positions = ", ".join(map(str, dirty_cells))
        raise NotebookError(
            f"{path.name}: clear outputs and execution counts in cells {positions}"
        )

    for cell_number, cell in enumerate(notebook.cells, start=1):
        if cell.cell_type == "code":
            _validate_idempotency(str(cell.source), path, cell_number)


def _frontmatter(
    *, metadata: dict[str, Any], slug: str, position: int, source_name: str
) -> str:
    fields = [
        "---",
        f"title: {json.dumps(metadata['title'])}",
        f"sidebar_label: {json.dumps(metadata['sidebar_label'])}",
        f"sidebar_position: {position + 1}",
        f"description: {json.dumps(metadata['description'])}",
        f"slug: /notebooks/{slug}",
        "keywords:",
        "  - Bursar tutorial",
        "  - Jupyter notebook",
    ]
    for keyword in metadata.get("keywords", []):
        fields.append(f"  - {keyword}")
    fields.extend(
        [
            (
                "custom_edit_url: "
                f"{REPOSITORY_URL}/edit/main/samples/python/notebooks/{source_name}"
            ),
            "---",
        ]
    )
    return "\n".join(fields)


def _source_notice(source_name: str) -> str:
    source_url = f"{REPOSITORY_URL}/blob/main/samples/python/notebooks/{source_name}"
    colab_url = (
        "https://colab.research.google.com/github/zonastery/bursar/blob/main/"
        f"samples/python/notebooks/{source_name}"
    )
    return (
        "{/* Generated by scripts/gen-notebook-docs.py; edit the source notebook. */}\n\n"
        ":::info Executable tutorial\n\n"
        f"This page is generated from a tested Jupyter notebook. "
        f"[Open it in Google Colab]({colab_url}) or "
        f"[view the source notebook]({source_url}).\n\n"
        ":::\n"
    )


def _write_categories() -> None:
    for section, values in SECTIONS.items():
        section_dir = OUTPUT_DIR / section
        section_dir.mkdir(parents=True, exist_ok=True)
        category = {
            "label": values["label"],
            "position": values["position"],
            "link": {
                "type": "generated-index",
                "title": values["title"],
                "description": values["description"],
            },
        }
        (section_dir / "_category_.json").write_text(
            json.dumps(category, indent=2) + "\n", encoding="utf-8"
        )


def _clean_generated_files() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUTPUT_DIR.rglob("*"):
        if path.is_file() and (path.suffix == ".mdx" or path.name == "_category_.json"):
            path.unlink()
    for path in sorted(OUTPUT_DIR.iterdir()):
        if path.is_dir() and path.name not in SECTIONS and not any(path.iterdir()):
            path.rmdir()


def main() -> int:
    _clean_generated_files()
    _write_categories()

    exporter = MarkdownExporter()
    notebooks = sorted(NOTEBOOK_DIR.glob("[0-9][0-9]_*.ipynb"))
    if not notebooks:
        raise NotebookError(f"No tutorial notebooks found in {NOTEBOOK_DIR}")

    for path in notebooks:
        match = NOTEBOOK_PATTERN.fullmatch(path.stem)
        if match is None:
            raise NotebookError(f"{path.name}: expected NN_snake_case.ipynb")

        notebook = nbformat.read(path, as_version=4)
        metadata = _metadata(notebook, path)
        _validate_notebook(notebook, path, metadata)

        body, _resources = exporter.from_notebook_node(notebook)
        slug = match.group("slug")
        position = int(match.group("position"))
        output = OUTPUT_DIR / metadata["section"] / f"{position:02}_{slug}.mdx"
        output.write_text(
            _frontmatter(
                metadata=metadata,
                slug=slug,
                position=position,
                source_name=path.name,
            )
            + "\n\n"
            + _source_notice(path.name)
            + "\n"
            + body.strip()
            + "\n",
            encoding="utf-8",
        )
        print(f"  {path.name} -> {output.relative_to(REPO_DIR)}")

    print(f"Generated {len(notebooks)} validated notebook tutorials")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NotebookError as error:
        print(f"gen-notebook-docs: {error}", file=sys.stderr)
        raise SystemExit(1) from error
