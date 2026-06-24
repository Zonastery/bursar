"""Aggregated cost breakdown produced by ``PricingEngine.calculate()``.

The ``CostBreakdown`` model holds per-category credit costs and
computes a ``total`` via a model validator.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class CostBreakdown(BaseModel):
    """Granular credit cost report for a usage event or batch.

    ``total`` is automatically computed from the component fields
    during initialisation and is capped at ``0.0`` from below.
    """

    model_credits: float = 0.0
    tool_credits: float = 0.0
    search_credits: float = 0.0
    cache_savings: float = 0.0
    fixed_credits: float = 0.0
    total: float = 0.0
    breakdown: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _compute_total(self) -> CostBreakdown:
        self.total = max(
            0.0,
            self.model_credits + self.tool_credits + self.search_credits + self.fixed_credits + self.cache_savings,
        )
        return self
