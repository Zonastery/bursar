"""Core engine that loads config and calculates credit costs.

The ``PricingEngine`` class is the main entry point for the bursar
package. It loads a validated ``PricingConfig`` from a dict or DB,
then calculates credit costs from ``UsageMetrics``.

Money safety (REFACTOR_CONTRACT §1): every cost is computed in
:class:`decimal.Decimal`. Components and the total are quantized to 4 dp
ROUND_HALF_UP at this boundary; the total is the single source of truth and
is clamped to ``>= 0`` exactly once. There is **no** integer truncation of
costs anywhere -- a 0.4-credit operation costs 0.4, not 0.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bursar.breakdown import CostBreakdown
from bursar.config import PricingConfig, load_config_from_dict
from bursar.expr import evaluate_expression
from bursar.metrics import METRIC_VARIABLES, UsageMetrics

__all__ = ["METRIC_VARIABLES", "PricingEngine"]

_QUANTUM = Decimal("0.0001")


def _q(value: Decimal) -> Decimal:
    """Quantize a credit amount to 4 dp using ROUND_HALF_UP (contract §1)."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


class PricingEngine:
    """Credit calculation engine.

    Usage::

        engine = PricingEngine.from_dict({
            "metering": {
                "models": {"*": "input_tokens * 0.001 + output_tokens * 0.003"},
            },
        })
        result = engine.calculate(UsageMetrics(
            model="claude-opus-4",
            input_tokens=1000,
            output_tokens=2000,
        ))
        print(result.total)  # Decimal('5.0000')
    """

    def __init__(self, config: PricingConfig) -> None:
        self._config = config

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PricingEngine":
        """Load engine from a config dictionary.

        Args:
            data: Dictionary representation of a pricing config.

        Returns:
            A new PricingEngine instance.

        Raises:
            ConfigError: If the config structure or expressions are invalid.
        """
        config = load_config_from_dict(data)
        return cls(config)

    def calculate(self, metrics: UsageMetrics) -> CostBreakdown:
        """Calculate credit cost for a single usage event.

        Args:
            metrics: Usage metrics including model, tokens, tool calls.

        Returns:
            CostBreakdown with per-dimension and total costs, all ``Decimal``
            quantized to 4 dp. ``total`` is clamped to ``>= 0``.

        Raises:
            ValueError: If the model is not found and no ``*``
                exists in the config.
            ExpressionError: If an expression evaluates unsafely (div/mod by
                zero, non-finite, overflow).
        """
        variables = self._build_variables(metrics)

        model_credits = self._calc_model(metrics.model, variables)
        tool_credits = self._calc_tools(metrics, variables)
        search_credits = self._calc_search(variables)
        cache_savings = self._calc_cache(variables)
        flat_job_credits = self._calc_flat_jobs(metrics)

        # Single source of truth: sum in exact Decimal, clamp to >= 0, quantize once.
        raw_total = model_credits + tool_credits + search_credits + cache_savings + flat_job_credits
        total = _q(max(Decimal(0), raw_total))

        return CostBreakdown(
            model_credits=_q(model_credits),
            tool_credits=_q(tool_credits),
            search_credits=_q(search_credits),
            cache_savings=_q(cache_savings),
            flat_job_credits=_q(flat_job_credits),
            total=total,
            breakdown={
                "model": metrics.model,
                "input_tokens": metrics.input_tokens,
                "output_tokens": metrics.output_tokens,
                "tool_count": len(metrics.tool_calls),
            },
        )

    def calculate_batch(self, metrics_list: list[UsageMetrics]) -> list[CostBreakdown]:
        """Calculate credit costs for multiple usage events.

        Args:
            metrics_list: List of usage metrics to calculate.

        Returns:
            List of CostBreakdown objects, one per input.
        """
        return [self.calculate(m) for m in metrics_list]

    def pricing_schema(self) -> dict[str, Any]:
        """Return the pricing config as a dictionary.

        Returns:
            Full pricing config as a dict (the same shape as the input).
        """
        return self._config.model_dump()

    @property
    def min_balance(self) -> Decimal:
        """Minimum balance users must keep (prevents spending last N credits)."""
        return self._config.ledger.min_balance

    def has_model(self, model_name: str) -> bool:
        """Check if a model name exists in the pricing config (exact match)."""
        return model_name in self._config.metering.models

    def resolve_model(self, model_version: str) -> str | None:
        """Resolve a model version string to a pricing config key.

        Tries exact match first, then prefix match
        (e.g. ``\"claude-sonnet-4-20250514\"`` -> ``\"claude-sonnet-4\"``).
        Returns ``None`` when no match exists.
        """
        models = self._config.metering.models
        if model_version in models:
            return model_version
        for key in models:
            if key != "*" and model_version.startswith(key):
                return key
        if "*" in models:
            return "*"
        return None

    def get_flat_job_cost(self, job_name: str) -> Decimal | None:
        """Get the flat credit cost for a named batch job.

        Returns ``None`` for an unknown / unconfigured job so callers (the
        manager) can reject it rather than silently charging 0 (L1). Values
        are already ``Decimal`` (fractional fixed costs are charged exactly).
        """
        flat_jobs = self._config.metering.flat_jobs
        if flat_jobs and job_name in flat_jobs:
            return _q(flat_jobs[job_name])
        return None

    def _build_variables(self, metrics: UsageMetrics) -> dict[str, int]:
        """Build variable dict from UsageMetrics. Keys == ``METRIC_VARIABLES``."""
        return {
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_write_tokens": metrics.cache_write_tokens,
            "tool_calls": len(metrics.tool_calls),
            "search_queries": metrics.search_queries,
            "search_results": metrics.search_results,
            "web_search_calls": metrics.web_search_calls,
            "code_exec_calls": metrics.code_exec_calls,
        }

    def _calc_model(self, model_name: str | None, variables: dict[str, int]) -> Decimal:
        """Evaluate model expression for the given model name."""
        if model_name is None or model_name == "none":
            model_name = "*"

        models = self._config.metering.models
        if model_name in models:
            expr = models[model_name]
        elif "*" in models:
            expr = models["*"]
        elif "_default" in models:
            expr = models["_default"]
        else:
            raise ValueError(f"no model match for '{model_name}' and no '*' or '_default' in config")

        return evaluate_expression(expr, variables)

    def _calc_tools(self, metrics: UsageMetrics, variables: dict[str, int]) -> Decimal:
        """Evaluate tool costs.

        Uses specific tool formula if available, falls back to ``*``.
        No double-counting when a specific override exists.

        Each branch gets its own ``calls`` count — the specific
        tool's own call count for a known-tool formula, or the unknown-call
        count for the ``*`` formula — while ``tool_calls`` in the base
        ``variables`` dict always stays the GLOBAL total across all tools and
        is never overridden here (WS2).
        """
        tools_config = self._config.metering.tools
        default_expr = tools_config.get("*", "calls * 0")
        total = Decimal(0)

        tool_names = {t.name for t in metrics.tool_calls}

        seen_specific = set()
        for tool_name in tool_names:
            if tool_name in tools_config:
                this_tool_count = sum(1 for t in metrics.tool_calls if t.name == tool_name)
                local_vars = dict(variables)
                local_vars["calls"] = this_tool_count
                total += evaluate_expression(tools_config[tool_name], local_vars)
                seen_specific.add(tool_name)

        unknown_tool_count = sum(1 for t in metrics.tool_calls if t.name not in seen_specific)
        if unknown_tool_count > 0:
            local_vars = dict(variables)
            local_vars["calls"] = unknown_tool_count
            total += evaluate_expression(default_expr, local_vars)

        return total

    def _calc_search(self, variables: dict[str, int]) -> Decimal:
        """Evaluate the search cost expression if configured."""
        if not self._config.metering.search:
            return Decimal(0)
        return evaluate_expression(self._config.metering.search, variables)

    def _calc_cache(self, variables: dict[str, int]) -> Decimal:
        """Evaluate the cache discount expression if configured.

        ``cache_discount`` is a positive number in config; the result is
        negated here so it acts as a saving (subtracted from the total).
        """
        if not self._config.metering.cache_discount:
            return Decimal(0)
        return -evaluate_expression(self._config.metering.cache_discount, variables)

    def _calc_flat_jobs(self, metrics: UsageMetrics) -> Decimal:
        """Lookup flat cost for a batch job, if applicable."""
        flat_jobs = self._config.metering.flat_jobs
        if not flat_jobs or not metrics.flat_job:
            return Decimal(0)
        job = metrics.flat_job
        if job in flat_jobs:
            return flat_jobs[job]
        return Decimal(0)
