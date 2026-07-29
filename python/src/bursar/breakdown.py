"""Aggregated cost breakdown produced by ``PricingEngine.calculate()``.

The ``CostBreakdown`` model holds per-category credit costs (all
:class:`decimal.Decimal`, quantized to 6 dp ROUND_HALF_UP) and a ``total``.

Single source of truth (M3): ``total`` is computed **once**, by the engine,
and passed in. The model no longer recomputes/overwrites it in a validator,
so there is exactly one place that decides clamping + rounding.

``makeCostBreakdown`` is the canonical factory — it re-derives the total
from the named components (sum, clamp to 0, quantize) and mirrors the JS
``makeCostBreakdown`` in ``breakdown.ts`` exactly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bursar.expr import quantize_money


class CostBreakdown(BaseModel):
    """Granular credit cost report for a usage event or batch.

    All monetary fields are :class:`decimal.Decimal`. The engine quantizes
    every field to 6 dp ROUND_HALF_UP and clamps ``total`` to ``>= 0`` before
    constructing this model; nothing here re-derives those numbers.
    """

    model_config = ConfigDict(extra="forbid")

    model_credits: Decimal = Decimal("0.000000")
    tool_credits: Decimal = Decimal("0.000000")
    search_credits: Decimal = Decimal("0.000000")
    cache_savings: Decimal = Decimal("0.000000")
    fixed_credits: Decimal = Decimal("0.000000")
    operation_credits: Decimal = Decimal("0.000000")
    total: Decimal = Decimal("0.000000")
    breakdown: dict[str, Any] = Field(default_factory=dict)


def make_cost_breakdown(
    *,
    model_credits: Decimal | None = None,
    tool_credits: Decimal | None = None,
    search_credits: Decimal | None = None,
    cache_savings: Decimal | None = None,
    fixed_credits: Decimal | None = None,
    operation_credits: Decimal | None = None,
    breakdown: dict[str, Any] | None = None,
) -> CostBreakdown:
    model_credits = quantize_money(model_credits if model_credits is not None else Decimal(0))
    tool_credits = quantize_money(tool_credits if tool_credits is not None else Decimal(0))
    search_credits = quantize_money(search_credits if search_credits is not None else Decimal(0))
    cache_savings = quantize_money(cache_savings if cache_savings is not None else Decimal(0))
    fixed_credits = quantize_money(fixed_credits if fixed_credits is not None else Decimal(0))
    operation_credits = quantize_money(operation_credits if operation_credits is not None else Decimal(0))

    raw_total = model_credits + tool_credits + search_credits + fixed_credits + operation_credits + cache_savings
    total = quantize_money(max(Decimal(0), raw_total))

    return CostBreakdown(
        model_credits=model_credits,
        tool_credits=tool_credits,
        search_credits=search_credits,
        cache_savings=cache_savings,
        fixed_credits=fixed_credits,
        operation_credits=operation_credits,
        total=total,
        breakdown=breakdown if breakdown is not None else {},
    )
