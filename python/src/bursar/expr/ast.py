"""Expression AST node types — mirrors JS SDK's ``expr/ast.ts``.

Python uses the built-in ``ast`` module for safe expression evaluation.
This module documents the expression grammar and allowed constructs.
"""

# The Python SDK leverages Python's built-in ``ast`` module for parsing
# and evaluating expressions. The AST nodes are standard Python ast nodes.
#
# Allowed node types: ast.Module, ast.Expression, ast.Expr, ast.BinOp,
# ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
# ast.Not, ast.USub, ast.UAdd, ast.Constant, ast.Load, ast.Name, ast.Call,
# ast.IfExp, ast.Compare, ast.BoolOp, ast.And, ast.Or, ast.Eq, ast.NotEq,
# ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn.
#
# Exponentiation (ast.Pow) is intentionally disallowed for DoS hardening.

ALLOWED_NODES = frozenset(
    {
        "Module",
        "Expression",
        "Expr",
        "BinOp",
        "UnaryOp",
        "Add",
        "Sub",
        "Mult",
        "Div",
        "FloorDiv",
        "Mod",
        "Not",
        "USub",
        "UAdd",
        "Constant",
        "Load",
        "Name",
        "Call",
        "IfExp",
        "Compare",
        "BoolOp",
        "And",
        "Or",
        "Eq",
        "NotEq",
        "Lt",
        "LtE",
        "Gt",
        "GtE",
        "In",
        "NotIn",
    }
)
