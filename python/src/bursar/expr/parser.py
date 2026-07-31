"""Expression parser — mirrors JS SDK's ``expr/parser.ts``.

The Python SDK delegates parsing to Python's built-in ``ast`` module.
This module wraps that with validation and Decimal literal rewriting.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal, InvalidOperation

from bursar.errors import ExpressionError
from bursar.expr.language import ALLOWED_FUNCTIONS, SAFE_NAMES


def _validate_ast(node: ast.AST) -> None:
    """Recursively validate AST nodes against the allowlist."""
    allowed = frozenset(
        {
            ast.Module,
            ast.Expression,
            ast.Expr,
            ast.BinOp,
            ast.UnaryOp,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.FloorDiv,
            ast.Mod,
            ast.Not,
            ast.USub,
            ast.UAdd,
            ast.Constant,
            ast.Load,
            ast.Name,
            ast.Call,
            ast.IfExp,
            ast.Compare,
            ast.BoolOp,
            ast.And,
            ast.Or,
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
            ast.In,
            ast.NotIn,
        }
    )
    node_type = type(node)
    if node_type is ast.Pow:
        raise ExpressionError("exponentiation ('**') is not allowed")
    if node_type not in allowed:
        raise ExpressionError(f"disallowed node type: {node_type.__name__}")

    if isinstance(node, ast.Call):
        func_name = node.func.id if isinstance(node.func, ast.Name) else None
        if func_name is None or func_name not in ALLOWED_FUNCTIONS:
            raise ExpressionError(f"unknown function: {func_name or 'non-name call'}")

    for child in ast.iter_child_nodes(node):
        _validate_ast(child)


_DECIMAL_CTOR = "_bursar_dec"
_IF_RE = re.compile(r"\bif\s*\(")


def make_decimal(literal: str) -> Decimal:
    """Construct an exact Decimal from a numeric literal's source text."""
    try:
        return Decimal(literal)
    except InvalidOperation as e:
        raise ExpressionError(f"invalid numeric literal: {literal!r}") from e


class DecimalLiteralTransformer(ast.NodeTransformer):
    """Rewrite numeric constants to Decimal constructor calls."""

    def __init__(self, source: str) -> None:
        self._source = source

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, bool):
            return node
        if isinstance(node.value, (int, float)):
            segment = ast.get_source_segment(self._source, node)
            if segment is None:
                segment = repr(node.value)
            make_decimal(segment)
            call = ast.Call(
                func=ast.Name(id=_DECIMAL_CTOR, ctx=ast.Load()),
                args=[ast.Constant(value=segment)],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


class InOperatorTransformer(ast.NodeTransformer):
    """Wrap In/NotIn operands in str() for JS-compatible String.includes."""

    def visit_Compare(self, node: ast.Compare) -> ast.Compare:
        self.generic_visit(node)
        for op_idx, op in enumerate(node.ops):
            if isinstance(op, (ast.In, ast.NotIn)):
                comparator = node.comparators[op_idx]
                str_comparator = ast.Call(
                    func=ast.Name(id="str", ctx=ast.Load()),
                    args=[comparator],
                    keywords=[],
                )
                str_left = ast.Call(
                    func=ast.Name(id="str", ctx=ast.Load()),
                    args=[node.left],
                    keywords=[],
                )
                node.comparators[op_idx] = str_left
                node.left = str_comparator
        return node


class NotPrecedenceTransformer(ast.NodeTransformer):
    """Fix Python 'not' precedence to apply to whole comparison."""

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.left, ast.UnaryOp) and isinstance(node.left.op, ast.Not):
            inner = ast.Compare(
                left=node.left.operand,
                ops=node.ops,
                comparators=node.comparators,
            )
            return ast.UnaryOp(op=ast.Not(), operand=inner)
        return node


def parse(source: str) -> tuple[ast.Expression, str]:
    """Parse and validate an expression string into an AST."""
    processed = _IF_RE.sub("_bursar_if(", source)
    try:
        tree = ast.parse(processed, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e}") from e

    _validate_ast(tree)

    variables_seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in SAFE_NAMES:
            variables_seen.add(node.id)

    if not variables_seen:
        raise ExpressionError("expression references no variables -- must use at least one metric")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in (_DECIMAL_CTOR, "str"):
            raise ExpressionError(f"'{node.id}' cannot be used as a bare variable — it is a function")

    return tree, processed


def rewrite(tree: ast.Expression, source: str) -> ast.Expression:
    """Apply all AST transformations: Decimal literals, not precedence, in/not-in."""
    tree = DecimalLiteralTransformer(source).visit(tree)
    tree = NotPrecedenceTransformer().visit(tree)
    tree = InOperatorTransformer().visit(tree)
    if not isinstance(tree, ast.Expression):
        raise ExpressionError("internal error: AST transformation did not produce an Expression node")
    ast.fix_missing_locations(tree)
    return tree
