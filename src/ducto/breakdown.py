"""Aggregated cost breakdown produced by ``PricingEngine.calculate()``.

The ``CostBreakdown`` dataclass holds per-category credit costs and
computes a ``total`` in ``__post_init__``.
"""

from dataclasses import dataclass, field


@dataclass
class CostBreakdown:
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
    breakdown: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.total = max(
            0.0,
            self.model_credits + self.tool_credits + self.search_credits + self.fixed_credits + self.cache_savings,
        )
