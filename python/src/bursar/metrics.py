"""Pure data structures for agent usage telemetry.

Consumed by ``PricingEngine.calculate()`` to produce a ``CostBreakdown``.

The expression namespace includes per-step ``METRIC_VARIABLES`` and a per-tool
``calls`` variable that counts invocations of the current tool.
"""

from pydantic import BaseModel, Field

# Canonical metric-variable set exposed to pricing expressions. This is the
# single authority used both to build the evaluation namespace (engine
# ``_build_variables``) and to validate expression variable names at
# config-load time (M5). Keep in sync with ``UsageMetrics`` / ``_build_variables``.
METRIC_VARIABLES: frozenset[str] = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "tool_calls",
        "search_queries",
        "search_results",
        "web_search_calls",
        "code_exec_calls",
    }
)


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
