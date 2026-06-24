"""Safe expression evaluator using Python's ast module.

Allows mathematical expressions with whitelisted variables and functions.
Rejects any AST node type not in the allowlist (no eval/exec).
"""

import ast
import builtins
import math
import re
from typing import Any

ALLOWED_FUNCTIONS = {"ceil", "floor", "min", "max", "round", "if", "tier", "clamp", "_ducto_if"}
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

# Custom whitelisted functions for expression evaluation


def _if(*args: Any) -> Any:
    if len(args) != 3:
        raise ExpressionError("if() requires exactly 3 arguments: if(condition, then, else)")
    return args[1] if args[0] else args[2]


def _tier(*args: float) -> float:
    """Tiered pricing: tier(val, t1, r1, t2, r2, ..., default).
    Returns r_i for first threshold where val < t_i, else default.
    """
    if len(args) < 3:
        raise ExpressionError("tier() requires at least 3 arguments (threshold + value pairs + default)")
    val = args[0]
    for i in range(1, len(args) - 1, 2):
        if val < args[i]:
            return args[i + 1]
    return args[-1]


def _clamp(*args: float) -> float:
    if len(args) != 3:
        raise ExpressionError("clamp() requires exactly 3 arguments: clamp(x, min, max)")
    return max(args[1], min(args[0], args[2]))


CUSTOM_FUNCTIONS = {"_ducto_if": _if, "tier": _tier, "clamp": _clamp}

# Names exempt from variable checks (functions + eval utilities)
_SAFE_NAMES: set[str] = ALLOWED_FUNCTIONS | {"str"}


def _build_namespace(variables: dict[str, float | int]) -> dict[str, Any]:
    """Build allowed namespace for expression evaluation."""
    ns: dict[str, Any] = {"__builtins__": {}}
    for name in MATH_FUNCTIONS:
        ns[name] = getattr(math, name)
    for name in BUILTIN_FUNCTIONS:
        ns[name] = getattr(builtins, name)
    ns.update(CUSTOM_FUNCTIONS)
    ns["str"] = builtins.str
    ns.update(variables)
    return ns


def _fix_in_operator(tree: ast.AST) -> ast.AST:
    """Wrap In/NotIn operands in str() so numbers work at runtime.
    JS evaluates 'in' as String(l).includes(String(r)).
    Python ast.In errors on number containers.
    To match JS: rewrite 'l in r' -> 'str(r) in str(l)'.
    """

    class InTransformer(ast.NodeTransformer):
        def visit_Compare(self, node: ast.Compare) -> ast.Compare:
            self.generic_visit(node)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.In, ast.NotIn)):
                    lineno = getattr(node, "lineno", 0)
                    col_offset = getattr(node, "col_offset", 0)
                    end_lineno = getattr(node, "end_lineno", lineno)
                    end_col_offset = getattr(node, "end_col_offset", col_offset)
                    # Create str(comparator) as new left -- wrapping right operand
                    new_left = ast.Call(
                        func=ast.Name(
                            id="str",
                            ctx=ast.Load(),
                            lineno=lineno,
                            col_offset=col_offset,
                            end_lineno=end_lineno,
                            end_col_offset=end_col_offset,
                        ),
                        args=[comparator],
                        keywords=[],
                        lineno=lineno,
                        col_offset=col_offset,
                        end_lineno=end_lineno,
                        end_col_offset=end_col_offset,
                    )
                    # Create str(node.left) as new comparator -- wrapping left operand
                    new_comp = ast.Call(
                        func=ast.Name(
                            id="str",
                            ctx=ast.Load(),
                            lineno=lineno,
                            col_offset=col_offset,
                            end_lineno=end_lineno,
                            end_col_offset=end_col_offset,
                        ),
                        args=[node.left],
                        keywords=[],
                        lineno=lineno,
                        col_offset=col_offset,
                        end_lineno=end_lineno,
                        end_col_offset=end_col_offset,
                    )
                    return ast.Compare(
                        left=new_left,
                        ops=[op],
                        comparators=[new_comp],
                        lineno=lineno,
                        col_offset=col_offset,
                        end_lineno=end_lineno,
                        end_col_offset=end_col_offset,
                    )
            # No In/NotIn op found -- pass through with position attributes
            return ast.Compare(
                left=node.left,
                ops=list(node.ops),
                comparators=list(node.comparators),
                lineno=getattr(node, "lineno", 0),
                col_offset=getattr(node, "col_offset", 0),
                end_lineno=getattr(node, "end_lineno", getattr(node, "lineno", 0)),
                end_col_offset=getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
            )

    return InTransformer().visit(tree)  # type: ignore[return-value, union-attr]


class _NotPrecedenceTransformer(ast.NodeTransformer):
    """Fix Python 'not' precedence: ensure not applies to whole comparison.

    When Python parses an expression like 'not x > 10' without parentheses,
    'not' binds to x only (not has lower precedence than comparisons in Python
    but we want it to apply to the whole comparison, matching user expectation).
    This transformer detects when a Compare's left operand is 'not X' and
    restructures to 'not (X op Y)'.
    """

    def visit_Compare(self, node: ast.Compare) -> ast.Compare:
        self.generic_visit(node)
        if isinstance(node.left, ast.UnaryOp) and isinstance(node.left.op, ast.Not):
            lineno = getattr(node, "lineno", 0)
            col_offset = getattr(node, "col_offset", 0)
            end_lineno = getattr(node, "end_lineno", lineno)
            end_col_offset = getattr(node, "end_col_offset", col_offset)
            inner = ast.Compare(
                left=node.left.operand,
                ops=node.ops,
                comparators=node.comparators,
                lineno=lineno,
                col_offset=col_offset,
                end_lineno=end_lineno,
                end_col_offset=end_col_offset,
            )
            return ast.UnaryOp(
                op=ast.Not(),
                operand=inner,
                lineno=lineno,
                col_offset=col_offset,
                end_lineno=end_lineno,
                end_col_offset=end_col_offset,
            )
        return node


def _fix_not_precedence(tree: ast.AST) -> ast.AST:
    return _NotPrecedenceTransformer().visit(tree)  # type: ignore[return-value, union-attr]


def validate_expression(expr: str) -> None:
    """Validate that an expression string is safe and syntactically valid.

    Args:
        expr: Expression string to validate.

    Raises:
        ExpressionError: If the expression contains disallowed constructs.

    """
    _validate_expression_tree(expr)


def _validate_expression_tree(expr: str) -> ast.Expression:
    """Parse and validate an expression string, returning the AST."""
    try:
        # Pre-process: 'if' is a Python keyword, rewrite to _ducto_if for parsing
        # This is safe because raw 'if' inside strings won't match the pattern
        processed = re.sub(r"if\s*\(", "_ducto_if(", expr)
        tree = ast.parse(processed, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e}") from e

    _validate_ast(tree)

    # Check that all referenced variables exist
    variables_seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _SAFE_NAMES:
            variables_seen.add(node.id)

    if not variables_seen:
        raise ExpressionError("expression references no variables -- must use at least one metric")

    return tree


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

    tree = _validate_expression_tree(expr)

    # Fix 'not' precedence so it applies to the whole comparison
    tree = _fix_not_precedence(tree)
    # Fix In/NotIn operators to str() both sides (match JS String.includes behavior)
    tree = _fix_in_operator(tree)
    assert isinstance(tree, ast.Expression), "tree must be an Expression after fix"

    # Check all referenced variables exist in provided variables
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _SAFE_NAMES and node.id not in variables:
            raise ExpressionError(f"undefined variable: '{node.id}'")

    namespace = _build_namespace(variables)
    code = compile(tree, "<expr>", "eval")
    try:
        result = eval(code, namespace)
    except ZeroDivisionError:
        return float("inf")
    return float(result)
