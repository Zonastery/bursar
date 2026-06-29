"""Safe expression evaluator using Python's ast module.

Allows mathematical expressions with whitelisted variables and functions.
Rejects any AST node type not in the allowlist (no eval/exec).

Money safety (REFACTOR_CONTRACT §1):
- All arithmetic is performed in :class:`decimal.Decimal`. Numeric literals are
  parsed as *exact* decimals from their source text (never via ``float()``), so
  ``0.1 + 0.2`` is exactly ``0.3``.
- Exponentiation (``**`` / ``ast.Pow``) is **rejected entirely** at validate
  time -- the simplest acceptable DoS fix per the contract (no constant-exponent
  carve-out). See ``ALLOWED_NODES`` (``ast.Pow`` is intentionally absent).
- Division / modulo by zero raise :class:`ExpressionError` (never ``inf``/``nan``).
- After evaluation the result is asserted finite; non-finite -> ``ExpressionError``.
- ``OverflowError`` / ``ValueError`` / ``InvalidOperation`` are converted to
  ``ExpressionError``.
"""

import ast
import math
import re
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

ALLOWED_FUNCTIONS = {"ceil", "floor", "min", "max", "round", "if", "tier", "clamp", "percentile", "_ducto_if"}

# Note: ``ast.Pow`` is deliberately NOT in this set -- exponentiation is rejected
# entirely (DoS hardening, C5). Rejection therefore surfaces as a "disallowed
# node type" error at validate time.
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
    if node_type is ast.Pow:
        # Explicit, friendly message for the most common rejection.
        raise ExpressionError("exponentiation ('**') is not allowed")
    if node_type not in ALLOWED_NODES:
        raise ExpressionError(f"disallowed node type: {node_type.__name__}")

    if isinstance(node, ast.Call):
        func_name = node.func.id if isinstance(node.func, ast.Name) else None
        if func_name is None or func_name not in ALLOWED_FUNCTIONS:
            raise ExpressionError(f"unknown function: {func_name or 'non-name call'}")

    for child in ast.iter_child_nodes(node):
        _validate_ast(child)


# ── Decimal-aware helper functions ────────────────────────────────────────


def _to_decimal(value: Any) -> Decimal:
    """Coerce a numeric (Decimal/int/bool) to Decimal exactly.

    Floats never reach here for literals (they are rewritten to Decimal during
    parsing) but variable values may still arrive as int/float, in which case we
    take the exact-from-string route to avoid binary float artefacts.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1) if value else Decimal(0)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise ExpressionError(f"non-numeric value in expression: {value!r}")


def _if(*args: Any) -> Any:
    if len(args) != 3:
        raise ExpressionError("if() requires exactly 3 arguments: if(condition, then, else)")
    return args[1] if args[0] else args[2]


def _tier(*args: Any) -> Decimal:
    """Tiered pricing: tier(val, t1, r1, t2, r2, ..., default).

    Returns r_i for the first threshold where val < t_i, else default.

    Arity (REFACTOR_CONTRACT §1, canonical rule): the form is
    ``value + N*(threshold, rate) pairs + default`` with N >= 1, so the total
    argument count is ``2N + 2`` -- **even and >= 4**. A single-tier table
    ``tier(x, 100, 1, 9)`` (4 args, N=1) is valid; odd arg counts (3/5/7) and
    fewer than 4 args are errors.
    """
    if len(args) < 4 or len(args) % 2 != 0:
        raise ExpressionError(
            "tier() requires an even number of arguments >= 4 (value, t1, r1, [t2, r2, ...], default)"
        )
    val = _to_decimal(args[0])
    for i in range(1, len(args) - 1, 2):
        if val < _to_decimal(args[i]):
            return _to_decimal(args[i + 1])
    return _to_decimal(args[-1])


def _clamp(*args: Any) -> Decimal:
    if len(args) != 3:
        raise ExpressionError("clamp() requires exactly 3 arguments: clamp(x, min, max)")
    x, lo, hi = _to_decimal(args[0]), _to_decimal(args[1]), _to_decimal(args[2])
    return max(lo, min(x, hi))


def _percentile(*args: Any) -> Decimal:
    """Compute the p-th percentile of values (p in 0..100).

    percentile(p, v1, v2, ...) -- sorts v1..vN and returns the value at the
    p-th percentile using linear interpolation. Requires >= 2 args and 0<=p<=100.
    """
    if len(args) < 2:
        raise ExpressionError("percentile() requires at least 2 arguments (p, v1, [v2, ...])")
    p = _to_decimal(args[0])
    if p < 0 or p > 100:
        raise ExpressionError("percentile() requires 0 <= p <= 100")
    values = sorted(_to_decimal(a) for a in args[1:])
    n = len(values)
    if n == 1:
        return values[0]
    rank = p / Decimal(100) * Decimal(n - 1)
    lower = int(rank.to_integral_value(rounding="ROUND_FLOOR"))
    upper = min(lower + 1, n - 1)
    frac = rank - Decimal(lower)
    return values[lower] * (Decimal(1) - frac) + values[upper] * frac


def _ceil(x: Any) -> Decimal:
    return Decimal(math.ceil(_to_decimal(x)))


def _floor(x: Any) -> Decimal:
    return Decimal(math.floor(_to_decimal(x)))


def _round(x: Any, ndigits: Any = None) -> Decimal:
    """Round half-up to ``ndigits`` decimals (default 0).

    Diverges from Python's banker's-rounding ``round()`` so both SDKs agree
    (ROUND_HALF_UP everywhere -- contract §1).
    """
    value = _to_decimal(x)
    if ndigits is None:
        return value.quantize(Decimal(1), rounding="ROUND_HALF_UP")
    n = int(_to_decimal(ndigits))
    quantum = Decimal(1).scaleb(-n)
    return value.quantize(quantum, rounding="ROUND_HALF_UP")


def _dmin(*args: Any) -> Decimal:
    if len(args) < 1:
        raise ExpressionError("min() requires at least 1 argument")
    return min(_to_decimal(a) for a in args)


def _dmax(*args: Any) -> Decimal:
    if len(args) < 1:
        raise ExpressionError("max() requires at least 1 argument")
    return max(_to_decimal(a) for a in args)


CUSTOM_FUNCTIONS: dict[str, Any] = {
    "_ducto_if": _if,
    "tier": _tier,
    "clamp": _clamp,
    "percentile": _percentile,
    "ceil": _ceil,
    "floor": _floor,
    "round": _round,
    "min": _dmin,
    "max": _dmax,
}

# Name of the namespace helper that builds an exact Decimal from a literal's
# source text. Underscore-prefixed and not in ALLOWED_FUNCTIONS, so an author
# cannot call it directly (it is injected only by the transformer).
_DECIMAL_CTOR = "_ducto_dec"

# Names exempt from variable checks (functions + eval utilities + injected
# Decimal constructor).
_SAFE_NAMES: set[str] = ALLOWED_FUNCTIONS | {"str", _DECIMAL_CTOR}


def _build_namespace(variables: dict[str, Any]) -> dict[str, Any]:
    """Build allowed namespace for expression evaluation (all Decimal-aware)."""
    ns: dict[str, Any] = {"__builtins__": {}}
    ns.update(CUSTOM_FUNCTIONS)
    ns["str"] = str
    ns[_DECIMAL_CTOR] = _make_decimal
    # Variable values are coerced to Decimal so all arithmetic stays exact.
    for name, value in variables.items():
        ns[name] = _to_decimal(value)
    return ns


def _make_decimal(literal: str) -> Decimal:
    """Construct an exact Decimal from a numeric literal's source text."""
    try:
        return Decimal(literal)
    except InvalidOperation as e:
        raise ExpressionError(f"invalid numeric literal: {literal!r}") from e


class _DecimalLiteralTransformer(ast.NodeTransformer):
    """Rewrite numeric ``Constant`` nodes to ``_ducto_dec("<literal-text>")``.

    ``ast.Constant`` stores the value already parsed as a binary ``float``, and
    ``compile()`` forbids a ``Decimal`` inside a ``Constant``. So instead we
    rewrite each numeric literal into a call to ``_make_decimal`` with the
    *original literal text* (e.g. ``"0.1"``), yielding an exact ``Decimal`` at
    runtime so money math never touches binary float.
    """

    def __init__(self, source: str) -> None:
        self._source = source

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, bool):
            # Keep booleans as-is (used by comparisons / if()).
            return node
        if isinstance(node.value, (int, float)):
            segment = ast.get_source_segment(self._source, node)
            if segment is None:
                # Fallback: reconstruct from repr (ints exact, floats via str()).
                segment = repr(node.value)
            # Validate eagerly so malformed literals fail fast.
            _make_decimal(segment)
            call = ast.Call(
                func=ast.Name(id=_DECIMAL_CTOR, ctx=ast.Load()),
                args=[ast.Constant(value=segment)],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


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
    """Fix Python 'not' precedence: ensure not applies to whole comparison."""

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
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


# Anchor the ``if(`` rewrite with ``\b`` so identifiers ending in ``if`` (e.g.
# ``qualif(x)``) are not mangled into ``qual_ducto_if(x)`` (M4).
_IF_RE = re.compile(r"\bif\s*\(")


def validate_expression(expr: str, known_variables: set[str] | None = None) -> None:
    """Validate that an expression string is safe and syntactically valid.

    Args:
        expr: Expression string to validate.
        known_variables: Optional canonical set of allowed variable names
            (the engine's metric set). When provided, any identifier that is
            neither a known variable nor an allowed function raises
            ``ExpressionError`` -- so config-author typos fail at config-load
            time rather than at first runtime evaluation (M5).

    Raises:
        ExpressionError: If the expression contains disallowed constructs or
            references an unknown variable.
    """
    _validate_expression_tree(expr, known_variables=known_variables)


def _validate_expression_tree(expr: str, known_variables: set[str] | None = None) -> tuple[ast.Expression, str]:
    """Parse and validate an expression string, returning (AST, processed source)."""
    try:
        # 'if' is a Python keyword; rewrite to _ducto_if for parsing.
        processed = _IF_RE.sub("_ducto_if(", expr)
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

    if known_variables is not None:
        unknown = variables_seen - known_variables
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ExpressionError(f"unknown variable(s): {names}")

    return tree, processed


def evaluate_expression(expr: str, variables: dict[str, Any]) -> Decimal:
    """Safely evaluate a validated expression in exact Decimal arithmetic.

    Args:
        expr: Expression string to evaluate.
        variables: Mapping of variable names to their numeric values.

    Returns:
        Exact ``Decimal`` result of the expression evaluation. Not quantized --
        callers (engine/breakdown) quantize at the cost boundary.

    Raises:
        ExpressionError: If the expression is invalid, references unknown
            variables, divides/mods by zero, overflows, or produces a
            non-finite result.
    """
    if not isinstance(variables, dict):
        raise ExpressionError("variables must be a dict")
    if not variables:
        raise ExpressionError("cannot evaluate: variables dict is empty")

    tree, processed = _validate_expression_tree(expr)

    # Rewrite numeric literals to exact Decimals (must run on the parsed tree).
    tree = _DecimalLiteralTransformer(processed).visit(tree)
    # Fix 'not' precedence so it applies to the whole comparison
    tree = _fix_not_precedence(tree)
    # Fix In/NotIn operators to str() both sides (match JS String.includes behavior)
    tree = _fix_in_operator(tree)
    assert isinstance(tree, ast.Expression), "tree must be an Expression after fix"
    ast.fix_missing_locations(tree)

    # Check all referenced variables exist in provided variables
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _SAFE_NAMES and node.id not in variables:
            raise ExpressionError(f"undefined variable: '{node.id}'")

    namespace = _build_namespace(variables)
    code = compile(tree, "<expr>", "eval")
    try:
        # Trap (rather than silently saturate) division/modulo by zero and any
        # arithmetic overflow inside a local Decimal context.
        with localcontext() as ctx:
            ctx.traps[DivisionByZero] = True
            ctx.traps[InvalidOperation] = True
            # eval over a validated AST + locked-down namespace (no builtins).
            result = eval(code, namespace)
    except ZeroDivisionError as e:
        # Covers both float ZeroDivisionError and decimal.DivisionByZero
        # (a subclass), e.g. ``x / 0`` and ``x // 0``.
        raise ExpressionError("division or modulo by zero") from e
    except (OverflowError, InvalidOperation) as e:
        raise ExpressionError(f"arithmetic error: {e}") from e
    except ValueError as e:
        raise ExpressionError(f"value error: {e}") from e

    result = _to_decimal(result)
    if not result.is_finite():
        raise ExpressionError("expression produced a non-finite result")
    return result
