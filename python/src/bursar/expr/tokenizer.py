"""Expression tokenizer — mirrors JS SDK's ``expr/tokenizer.ts``.

The Python SDK relies on Python's built-in ``ast`` module for parsing
rather than implementing a custom tokenizer. This module exists for
structural parity with the JS SDK.
"""

from __future__ import annotations

import ast


def tokenize(expr: str) -> list[str]:
    """Minimal tokenizer — for parity only.

    The actual parsing is done by Python's ``ast`` module.
    This function is provided for debugging and introspection.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []

    tokens: list[str] = []
    _walk(tokens, tree)
    return tokens


def _walk(tokens: list[str], node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _walk(tokens, node.body)
    elif isinstance(node, ast.Constant):
        tokens.append(repr(node.value))
    elif isinstance(node, ast.Name):
        tokens.append(node.id)
    elif isinstance(node, ast.BinOp):
        _walk(tokens, node.left)
        tokens.append(_op_name(node.op))
        _walk(tokens, node.right)
    elif isinstance(node, ast.UnaryOp):
        tokens.append(_op_name(node.op))
        _walk(tokens, node.operand)
    elif isinstance(node, ast.Call):
        _walk(tokens, node.func)
        tokens.append("(")
        for i, arg in enumerate(node.args):
            if i > 0:
                tokens.append(",")
            _walk(tokens, arg)
        tokens.append(")")


def _op_name(op: ast.AST) -> str:
    return type(op).__name__.replace("_", "")
