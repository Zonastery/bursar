"""Safe expression evaluation — mirrors JS SDK's ``expr.ts`` facade.

Re-exports the expression validation and evaluation API from the ``expr/``
subpackage.
"""

from __future__ import annotations

from bursar.expr.evaluator import (
    ExpressionError,
    evaluate,
    validate,
)

# Re-export with the canonical names used throughout the codebase.
evaluate_expression = evaluate
validate_expression = validate


__all__ = [
    "ExpressionError",
    "evaluate_expression",
    "validate_expression",
]
