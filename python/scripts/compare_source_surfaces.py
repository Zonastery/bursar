"""Report normalized declaration/member differences between SDK source inventories."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def canonical_name(name: str) -> str:
    """Normalize Pascal/camel/snake names without splitting acronym runs."""
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.replace("-", "_").lower()


def grouped_surface(path: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for declaration in json.loads(Path(path).read_text(encoding="utf-8")):
        if declaration["exported"]:
            grouped[canonical_name(declaration["name"])].append(declaration)
    return grouped


def public_members(declarations: list[dict[str, Any]]) -> set[str]:
    return {
        canonical_name(member["name"])
        for declaration in declarations
        for member in declaration["members"]
        if member["visibility"] == "public"
    }


def public_member_optionalities(declarations: list[dict[str, Any]]) -> dict[str, set[bool]]:
    result: dict[str, set[bool]] = defaultdict(set)
    for declaration in declarations:
        for member in declaration["members"]:
            if member["visibility"] == "public":
                result[canonical_name(member["name"])].add(bool(member["optional"]))
    return result


def describe(declarations: list[dict[str, Any]]) -> str:
    return ", ".join(f"{item['name']} ({item['file']})" for item in declarations)


def main(javascript_path: str, python_path: str) -> None:
    javascript = grouped_surface(javascript_path)
    python = grouped_surface(python_path)
    print(f"exported declarations: JavaScript={len(javascript)} Python={len(python)}")
    print("\nJavaScript-only declarations")
    for name in sorted(javascript.keys() - python.keys()):
        print(f"  {name}: {describe(javascript[name])}")
    print("\nPython-only declarations")
    for name in sorted(python.keys() - javascript.keys()):
        print(f"  {name}: {describe(python[name])}")
    print("\nMatched declarations with member differences")
    for name in sorted(javascript.keys() & python.keys()):
        javascript_members = public_members(javascript[name])
        python_members = public_members(python[name])
        javascript_optional = public_member_optionalities(javascript[name])
        python_optional = public_member_optionalities(python[name])
        requiredness_differences = [
            member
            for member in sorted(javascript_members & python_members)
            if javascript_optional[member] != python_optional[member]
        ]
        if javascript_members == python_members and not requiredness_differences:
            continue
        print(f"  {name}")
        if javascript_members - python_members:
            print(f"    JavaScript-only: {', '.join(sorted(javascript_members - python_members))}")
        if python_members - javascript_members:
            print(f"    Python-only: {', '.join(sorted(python_members - javascript_members))}")
        if requiredness_differences:
            print(f"    Requiredness differs: {', '.join(requiredness_differences)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
