"""Expression evaluator — mirrors JS SDK's ``expr/evaluator.ts``.

Evaluates validated expression ASTs in exact Decimal arithmetic with a
locked-down namespace (no builtins).
"""

from __future__ import annotations

import ast
import math
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

from bursar.expr.language import ALLOWED_FUNCTIONS
from bursar.expr.parser import (
    _DECIMAL_CTOR,
    ExpressionError,
    make_decimal,
    parse,
    rewrite,
)


def _to_decimal(value: Any) -> Decimal:
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
    "_bursar_if": _if,
    "tier": _tier,
    "clamp": _clamp,
    "percentile": _percentile,
    "ceil": _ceil,
    "floor": _floor,
    "round": _round,
    "min": _dmin,
    "max": _dmax,
}


def _build_namespace(variables: dict[str, Any]) -> dict[str, Any]:
    ns: dict[str, Any] = {"__builtins__": {}}
    ns.update(CUSTOM_FUNCTIONS)
    ns["str"] = str
    ns[_DECIMAL_CTOR] = make_decimal
    for name, value in variables.items():
        ns[name] = _to_decimal(value)
    return ns


def evaluate(expr: str, variables: dict[str, Any]) -> Decimal:
    """Safely evaluate a validated expression in exact Decimal arithmetic.

    Args:
        expr: Expression string to evaluate.
        variables: Mapping of variable names to their numeric values.

    Returns:
        Exact ``Decimal`` result of the expression evaluation.

    Raises:
        ExpressionError: If the expression is invalid or evaluation fails.
    """
    if not isinstance(variables, dict):
        raise ExpressionError("variables must be a dict")
    if not variables:
        raise ExpressionError("cannot evaluate: variables dict is empty")

    tree, processed = parse(expr)
    tree = rewrite(tree, processed)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id not in ALLOWED_FUNCTIONS | {"str", _DECIMAL_CTOR}
            and node.id not in variables
        ):
            raise ExpressionError(f"undefined variable: '{node.id}'")

    namespace = _build_namespace(variables)
    code = compile(tree, "<expr>", "eval")
    try:
        with localcontext() as ctx:
            ctx.traps[DivisionByZero] = True
            ctx.traps[InvalidOperation] = True
            # Parsing, AST validation, rewriting, and the locked namespace above are the evaluator's security boundary.
            result = eval(code, namespace)  # noqa: S307
    except ZeroDivisionError as e:
        raise ExpressionError("division or modulo by zero") from e
    except (OverflowError, InvalidOperation, TypeError) as e:
        raise ExpressionError(f"arithmetic error: {e}") from e
    except ValueError as e:
        raise ExpressionError(f"value error: {e}") from e

    result = _to_decimal(result)
    if not result.is_finite():
        raise ExpressionError("expression produced a non-finite result")
    return result


def validate(expr: str, known_variables: set[str]) -> None:
    """Validate that an expression string is safe and syntactically valid.

    Args:
        expr: Expression string to validate.
        known_variables: Canonical set of allowed variable names.

    Raises:
        ExpressionError: If the expression contains disallowed constructs.
    """
    tree, _processed = parse(expr)

    variables_seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCTIONS | {"str", _DECIMAL_CTOR}:
            variables_seen.add(node.id)

    if not variables_seen:
        raise ExpressionError("expression references no variables -- must use at least one metric")
    unknown = variables_seen - known_variables
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ExpressionError(f"unknown variable(s): {names}")
