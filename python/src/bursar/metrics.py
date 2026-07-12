"""Pure data structures for agent usage telemetry.

Consumed by ``PricingEngine.calculate()`` to produce a ``CostBreakdown``.

The expression namespace includes per-step ``METRIC_VARIABLES`` and a per-tool
``calls`` variable that counts invocations of the current tool.
"""

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A single tool invocation recorded during an agent step."""

    name: str


class UsageMetrics(BaseModel):
    """Raw usage counters collected across one or more agent steps.

    All integer fields default to 0 so callers can partially populate
    the struct and rely on sensible zero-values.

    The ``calls`` variable is available only in tool-scoped expressions
    and reflects the number of times the current tool was invoked.
    """

    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: list[ToolCall] = Field(default_factory=list)
    search_queries: int = 0
    search_results: int = 0
    web_search_calls: int = 0
    code_exec_calls: int = 0
    flat_job: str | None = None


# Canonical metric-variable set exposed to pricing expressions. This is the
# single authority used both to build the evaluation namespace (engine
# ``_build_variables``) and to validate expression variable names at
# config-load time (M5). Derived from ``UsageMetrics.model_fields`` so it
# stays in sync automatically.
METRIC_VARIABLES: frozenset[str] = frozenset(
    name for name, field in UsageMetrics.model_fields.items() if name not in ("model", "flat_job")
)
