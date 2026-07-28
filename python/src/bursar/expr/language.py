"""Expression language definitions — mirrors JS SDK's ``expr/language.ts``.

Defines the supported functions and variables available in pricing expressions.
"""

from __future__ import annotations

ALLOWED_FUNCTIONS = frozenset(
    {
        "ceil",
        "floor",
        "min",
        "max",
        "round",
        "tier",
        "clamp",
        "percentile",
        "_bursar_if",
    }
)

ALLOWED_FUNCTION_ARGS: dict[str, tuple[int, int | None]] = {
    "ceil": (1, 1),
    "floor": (1, 1),
    "min": (1, None),
    "max": (1, None),
    "round": (1, 2),
    "tier": (4, None),
    "clamp": (3, 3),
    "percentile": (2, None),
    "_bursar_if": (3, 3),
}

SAFE_NAMES: set[str] = ALLOWED_FUNCTIONS | {"str"}
