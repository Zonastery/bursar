"""Safe expression evaluator using Python's ast module.

Allows mathematical expressions with whitelisted variables and functions.
Rejects any AST node type not in the allowlist (no eval/exec).
"""

import ast
import builtins
import math
from typing import Any

ALLOWED_FUNCTIONS = {"ceil", "floor", "min", "max", "round"}
ALLOWED_NODES = frozenset(
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
        ast.Pow,
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


class ExpressionError(Exception):
    """Raised on invalid or unsafe expressions."""


def _validate_ast(node: ast.AST) -> None:
    """Recursively validate AST nodes against the allowlist."""
    node_type = type(node)
    if node_type not in ALLOWED_NODES:
        raise ExpressionError(f"disallowed node type: {node_type.__name__}")

    if isinstance(node, ast.Call):
        func_name = node.func.id if isinstance(node.func, ast.Name) else None
        if func_name is None or func_name not in ALLOWED_FUNCTIONS:
            raise ExpressionError(f"unknown function: {func_name or 'non-name call'}")

    for child in ast.iter_child_nodes(node):
        _validate_ast(child)


MATH_FUNCTIONS = {"ceil", "floor"}
BUILTIN_FUNCTIONS = {"min", "max", "round"}


def _build_namespace(variables: dict[str, float | int]) -> dict[str, Any]:
    """Build allowed namespace for expression evaluation."""
    ns: dict[str, Any] = {"__builtins__": {}}
    for name in MATH_FUNCTIONS:
        ns[name] = getattr(math, name)
    for name in BUILTIN_FUNCTIONS:
        ns[name] = getattr(builtins, name)
    ns.update(variables)
    return ns


def validate_expression(expr: str) -> None:
    """Validate that an expression string is safe and syntactically valid.

    Args:
        expr: Expression string to validate.

    Raises:
        ExpressionError: If the expression contains disallowed constructs.

    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e}") from e

    _validate_ast(tree)

    # Check that all referenced variables exist
    variables_seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCTIONS:
            variables_seen.add(node.id)

    if not variables_seen:
        raise ExpressionError("expression references no variables -- must use at least one metric")


def evaluate_expression(expr: str, variables: dict[str, float | int]) -> float:
    """Safely evaluate a validated expression.

    Args:
        expr: Expression string to evaluate.
        variables: Mapping of variable names to their numeric values.

    Returns:
        Numeric result of the expression evaluation.

    Raises:
        ExpressionError: If the expression is invalid or references
            unknown variables.

    """
    if not variables:
        raise ExpressionError("cannot evaluate: variables dict is empty")
    if not isinstance(variables, dict):
        raise ExpressionError("variables must be a dict")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e}") from e

    _validate_ast(tree)

    # Check all referenced variables exist in provided variables
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCTIONS and node.id not in variables:
            raise ExpressionError(f"undefined variable: '{node.id}'")

    namespace = _build_namespace(variables)
    code = compile(tree, "<expr>", "eval")
    try:
        result = eval(code, namespace)
    except ZeroDivisionError:
        return float("inf")
    return float(result)
