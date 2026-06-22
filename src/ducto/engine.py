"""Core engine that loads config and calculates credit costs.

The ``PricingEngine`` class is the main entry point for the ducto
package. It loads a validated ``PricingConfig`` from a dict or DB,
then calculates credit costs from ``UsageMetrics``.
"""

from ducto.breakdown import CostBreakdown
from ducto.config import PricingConfig, load_config_from_dict
from ducto.expr import evaluate_expression
from ducto.interface.models import PricingConfigData
from ducto.metrics import UsageMetrics


def _safe_total(value: float) -> float:
    """Clamp negative values to zero, keeping 2-decimal precision."""
    return max(0.0, round(value, 2))


class PricingEngine:
    """Credit calculation engine.

    Usage::

        engine = PricingEngine.from_dict({
            "version": 1,
            "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
        })
        result = engine.calculate(UsageMetrics(
            model="claude-opus-4",
            input_tokens=1000,
            output_tokens=2000,
        ))
        print(result.total)  # 35.0
    """

    def __init__(self, config: PricingConfig) -> None:
        self._config = config

    @classmethod
    def from_dict(cls, data: dict) -> "PricingEngine":
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
            CostBreakdown with per-dimension and total costs.

        Raises:
            ValueError: If the model is not found and no ``_default``
                exists in the config.
        """
        variables = self._build_variables(metrics)

        model_credits = self._calc_model(metrics.model, variables)
        tool_credits = self._calc_tools(metrics, variables)
        search_credits = self._calc_search(variables)
        cache_savings = self._calc_cache(variables)
        fixed_credits = self._calc_fixed(metrics)

        total = _safe_total(model_credits + tool_credits + search_credits + cache_savings + fixed_credits)
        model_credits = _safe_total(model_credits)
        tool_credits = _safe_total(tool_credits)
        search_credits = _safe_total(search_credits)
        cache_savings = round(cache_savings, 2)
        fixed_credits = float(fixed_credits)

        return CostBreakdown(
            model_credits=model_credits,
            tool_credits=tool_credits,
            search_credits=search_credits,
            cache_savings=cache_savings,
            fixed_credits=fixed_credits,
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

    def pricing_schema(self) -> PricingConfigData:
        """Return the pricing config as a typed model.

        Returns:
            ``PricingConfigData`` with all pricing sections and expressions.
        """
        return PricingConfigData(
            version=self._config.version,
            models=dict(self._config.models),
            tools=dict(self._config.tools),
            search=dict(self._config.search) if self._config.search else None,
            cache=dict(self._config.cache) if self._config.cache else None,
            min_balance=self._config.min_balance,
            fixed=dict(self._config.fixed) if self._config.fixed else None,
        )

    @property
    def min_balance(self) -> int:
        """Minimum balance users must keep (prevents spending last N credits)."""
        return self._config.min_balance

    def has_model(self, model_name: str) -> bool:
        """Check if a model name exists in the pricing config (exact match)."""
        return model_name in self._config.models

    def resolve_model(self, model_version: str) -> str | None:
        """Resolve a model version string to a pricing config key.

        Tries exact match first, then prefix match
        (e.g. ``\"claude-sonnet-4-20250514\"`` -> ``\"claude-sonnet-4\"``).
        Returns ``None`` when no match exists.
        """
        if model_version in self._config.models:
            return model_version
        for key in self._config.models:
            if key != "_default" and model_version.startswith(key):
                return key
        if "_default" in self._config.models:
            return "_default"
        return None

    def get_fixed_cost(self, job_name: str) -> int | None:
        """Get the fixed credit cost for a named batch job."""
        if self._config.fixed and job_name in self._config.fixed:
            return int(self._config.fixed[job_name])
        return None

    def _build_variables(self, metrics: UsageMetrics) -> dict[str, float | int]:
        """Build variable dict from UsageMetrics."""
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

    def _calc_model(self, model_name: str | None, variables: dict) -> float:
        """Evaluate model expression for the given model name."""
        if model_name is None or model_name == "none":
            model_name = "_default"

        models = self._config.models
        if model_name in models:
            expr = models[model_name]
        elif "_default" in models:
            expr = models["_default"]
        else:
            raise ValueError(f"no model match for '{model_name}' and no _default in config")

        return evaluate_expression(expr, variables)

    def _calc_tools(self, metrics: UsageMetrics, variables: dict) -> float:
        """Evaluate tool costs.

        Uses specific tool formula if available, falls back to _default.
        No double-counting when a specific override exists.
        """
        tools_config = self._config.tools
        default_expr = tools_config.get("_default", "tool_calls * 0")
        total = 0.0

        tool_names = {t.name for t in metrics.tool_calls}

        seen_specific = set()
        for tool_name in tool_names:
            if tool_name in tools_config:
                total += evaluate_expression(tools_config[tool_name], variables)
                seen_specific.add(tool_name)

        unknown_tool_count = sum(1 for t in metrics.tool_calls if t.name not in seen_specific)
        if unknown_tool_count > 0:
            local_vars = dict(variables)
            local_vars["tool_calls"] = unknown_tool_count
            total += evaluate_expression(default_expr, local_vars)

        return total

    def _calc_search(self, variables: dict) -> float:
        """Evaluate search cost expression if configured."""
        if not self._config.search or "costs" not in self._config.search:
            return 0.0
        return evaluate_expression(self._config.search["costs"], variables)

    def _calc_cache(self, variables: dict) -> float:
        """Evaluate cache discount expression if configured."""
        if not self._config.cache or "discount" not in self._config.cache:
            return 0.0
        return evaluate_expression(self._config.cache["discount"], variables)

    def _calc_fixed(self, metrics: UsageMetrics) -> float:
        """Lookup fixed cost for a batch job, if applicable."""
        if not self._config.fixed or not metrics.fixed_job:
            return 0.0
        job = metrics.fixed_job
        if job in self._config.fixed:
            return float(self._config.fixed[job])
        return 0.0
