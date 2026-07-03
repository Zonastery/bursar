"""Aggregated cost breakdown produced by ``PricingEngine.calculate()``.

The ``CostBreakdown`` model holds per-category credit costs (all
:class:`decimal.Decimal`, quantized to 4 dp ROUND_HALF_UP) and a ``total``.

Single source of truth (M3): ``total`` is computed **once**, by the engine,
and passed in. The model no longer recomputes/overwrites it in a validator,
so there is exactly one place that decides clamping + rounding.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CostBreakdown(BaseModel):
    """Granular credit cost report for a usage event or batch.

    All monetary fields are :class:`decimal.Decimal`. The engine quantizes
    every field to 4 dp ROUND_HALF_UP and clamps ``total`` to ``>= 0`` before
    constructing this model; nothing here re-derives those numbers.
    """

    model_config = ConfigDict(extra="forbid")

    model_credits: Decimal = Decimal("0.0000")
    tool_credits: Decimal = Decimal("0.0000")
    search_credits: Decimal = Decimal("0.0000")
    cache_savings: Decimal = Decimal("0.0000")
    fixed_credits: Decimal = Decimal("0.0000")
    total: Decimal = Decimal("0.0000")
    breakdown: dict[str, Any] = Field(default_factory=dict)
