"""Safe expression evaluation — mirrors JS SDK's ``expr.ts`` facade.

Re-exports the expression validation and evaluation API from the ``expr/``
subpackage.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from bursar.expr.evaluator import (
    ExpressionError,
    evaluate,
    validate,
)

# Re-export with the canonical names used throughout the codebase.
evaluate_expression = evaluate
validate_expression = validate

_MONEY_DECIMAL_PLACES = 6
_MONEY_QUANTUM = Decimal("1e-6")


def quantize_money(value: Decimal) -> Decimal:
    """Quantize a Decimal credit amount to 6dp using ROUND_HALF_UP."""
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "ExpressionError",
    "evaluate_expression",
    "quantize_money",
    "validate_expression",
]
