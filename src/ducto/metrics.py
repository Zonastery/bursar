"""Pure data structures for agent usage telemetry.

Consumed by ``PricingEngine.calculate()`` to produce a ``CostBreakdown``.
"""

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A single tool invocation recorded during an agent step."""

    name: str


@dataclass
class UsageMetrics:
    """Raw usage counters collected across one or more agent steps.

    All integer fields default to 0 so callers can partially populate
    the struct and rely on sensible zero-values.
    """

    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    search_queries: int = 0
    search_results: int = 0
    web_search_calls: int = 0
    code_exec_calls: int = 0
    fixed_job: str | None = None
