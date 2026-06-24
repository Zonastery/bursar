"""Pricing config loading with pydantic validation and expression validation."""

from typing import Any

from pydantic import BaseModel, Field, NonNegativeInt, model_validator

from ducto.expr import ExpressionError, validate_expression
from ducto.interface.models import PlanDefinition


class ConfigError(Exception):
    """Raised on config parsing or validation failures."""


class PricingConfig(BaseModel):
    """Validated pricing configuration.

    Unified format (no version field).  Optional ``plans`` key carries
    subscription-plan definitions for allowance-based features.
    """

    models: dict[str, str]
    tools: dict[str, str] = Field(default_factory=lambda: {"_default": "tool_calls * 0"})
    search: dict[str, str] = Field(default_factory=dict)
    cache: dict[str, str] = Field(default_factory=dict)
    min_balance: int = Field(default=5, ge=0)
    fixed: dict[str, NonNegativeInt] = Field(default_factory=dict)
    plans: dict[str, PlanDefinition] | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_structure(cls, data: Any) -> Any:
        """Validate top-level structure before field validation."""
        if not isinstance(data, dict):
            return data
        if "models" not in data:
            raise ConfigError("missing required section: models")
        if not isinstance(data["models"], dict) or len(data["models"]) == 0:
            raise ConfigError("models must be a non-empty dict")
        plans = data.get("plans")
        if plans is not None and isinstance(plans, dict):
            plan_names = [p["name"] if isinstance(p, dict) else p.name for p in plans.values()]
            if len(plan_names) != len(set(plan_names)):
                raise ConfigError("duplicate plan names in pricing config")
        return data

    @model_validator(mode="after")
    def validate_expressions(self) -> "PricingConfig":
        """Validate all expression strings in the config."""
        for model_name, expr in self.models.items():
            try:
                validate_expression(expr)
            except ExpressionError as e:
                raise ConfigError(f"invalid expression in models.{model_name}: {e}") from e

        for tool_name, expr in self.tools.items():
            try:
                validate_expression(expr)
            except ExpressionError as e:
                raise ConfigError(f"invalid expression in tools.{tool_name}: {e}") from e

        for section_name, section in [
            ("search", self.search),
            ("cache", self.cache),
        ]:
            for key, expr in section.items():
                try:
                    validate_expression(expr)
                except ExpressionError as e:
                    raise ConfigError(f"invalid expression in {section_name}.{key}: {e}") from e

        return self


def load_config_from_dict(data: dict) -> PricingConfig:
    """Load and validate a pricing config from a dictionary.

    Args:
        data: Dictionary representation of a pricing config.

    Returns:
        Validated PricingConfig instance.

    Raises:
        ConfigError: If the config structure or expressions are invalid.
    """
    return PricingConfig.model_validate(data)
