"""Export Python source declarations for the cross-SDK parity audit."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "bursar"


def _parameter(parameter: ast.arg, optional: bool, *, rest: bool = False) -> dict[str, Any]:
    return {"name": parameter.arg, "optional": optional, "rest": rest}


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
    positional = [*node.args.posonlyargs, *node.args.args]
    default_start = len(positional) - len(node.args.defaults)
    result = [
        _parameter(parameter, index >= default_start)
        for index, parameter in enumerate(positional)
        if parameter.arg not in {"self", "cls"}
    ]
    if node.args.vararg:
        result.append(_parameter(node.args.vararg, True, rest=True))
    kw_default_start = len(node.args.kwonlyargs) - len(node.args.kw_defaults)
    for index, parameter in enumerate(node.args.kwonlyargs):
        default = node.args.kw_defaults[index]
        result.append(_parameter(parameter, index >= kw_default_start and default is not None))
    if node.args.kwarg:
        result.append(_parameter(node.args.kwarg, True, rest=True))
    return result


def _decorator_name(decorator: ast.expr) -> str | None:
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    if isinstance(decorator, ast.Call):
        return _decorator_name(decorator.func)
    return None


def _assignment_optional(value: ast.expr | None) -> bool:
    if value is None:
        return False
    if not isinstance(value, ast.Call) or _decorator_name(value.func) != "Field":
        return True
    if value.args:
        return not (
            len(value.args) == 1 and isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis
        )
    defaults = {
        keyword.arg: keyword.value for keyword in value.keywords if keyword.arg in {"default", "default_factory"}
    }
    if "default_factory" in defaults:
        return True
    default = defaults.get("default")
    return default is not None and not (isinstance(default, ast.Constant) and default.value is Ellipsis)


def _members(node: ast.ClassDef) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for member in node.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorator_names = {
                name for decorator in member.decorator_list if (name := _decorator_name(decorator)) is not None
            }
            if (
                member.name.startswith("__")
                and member.name.endswith("__")
                or decorator_names & {"field_validator", "model_validator"}
            ):
                continue
            result.append(
                {
                    "name": member.name,
                    "kind": "getter" if "property" in decorator_names else "method",
                    "visibility": "private" if member.name.startswith("_") else "public",
                    "static": bool(decorator_names & {"staticmethod", "classmethod"}),
                    "optional": False,
                    "parameters": _parameters(member),
                }
            )
        elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
            result.append(
                {
                    "name": member.target.id,
                    "kind": "property",
                    "visibility": "private" if member.target.id.startswith("_") else "public",
                    "static": False,
                    "optional": _assignment_optional(member.value),
                    "parameters": [],
                }
            )
        elif isinstance(member, ast.Assign):
            for target in member.targets:
                if isinstance(target, ast.Name):
                    result.append(
                        {
                            "name": target.id,
                            "kind": "property",
                            "visibility": ("private" if target.id.startswith("_") else "public"),
                            "static": True,
                            "optional": False,
                            "parameters": [],
                        }
                    )
    return result


def _declarations(path: Path) -> list[dict[str, Any]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative_path = str(path.relative_to(SOURCE_ROOT))
    result: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            result.append(
                {
                    "file": relative_path,
                    "name": node.name,
                    "kind": "class",
                    "exported": not node.name.startswith("_"),
                    "members": _members(node),
                }
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append(
                {
                    "file": relative_path,
                    "name": node.name,
                    "kind": "function",
                    "exported": not node.name.startswith("_"),
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "parameters": _parameters(node),
                    "members": [],
                }
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    result.append(
                        {
                            "file": relative_path,
                            "name": target.id,
                            "kind": "variable",
                            "exported": not target.id.startswith("_"),
                            "members": [],
                        }
                    )
    return result


def main(output: str) -> None:
    declarations = [
        declaration
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
        for declaration in _declarations(path)
    ]
    Path(output).write_text(json.dumps(declarations, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
