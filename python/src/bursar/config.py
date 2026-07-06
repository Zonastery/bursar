"""Pricing config loading with pydantic validation and expression validation."""

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bursar.expr import ExpressionError, validate_expression
from bursar.interface.models import PlanDefinition, TierDefinition
from bursar.metrics import METRIC_VARIABLES


class ConfigError(Exception):
    """Raised on config parsing or validation failures."""


class PricingConfig(BaseModel):
    """Validated pricing configuration.

    ``version`` must be ``1`` (current/latest).  Optional ``plans`` key carries
    subscription-plan definitions for allowance-based features.
    """

    # Unknown top-level keys (typos like `min_balnce`) fail loudly instead of
    # silently falling back to a default.
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    models: dict[str, str]
    tools: dict[str, str] = Field(default_factory=lambda: {"_default": "tool_calls * 0"})
    search: str | None = None
    cache: str | None = None
    # Money field: fractional credits, never float (contract §1).
    min_balance: Decimal = Field(default=Decimal(0), ge=0)
    signup_bonus: int = Field(default=50, ge=0)
    fixed: dict[str, Decimal] = Field(default_factory=dict)
    plans: dict[str, PlanDefinition] | None = None
    tiers: dict[str, TierDefinition] | None = None

    @field_validator("fixed")
    @classmethod
    def validate_fixed_non_negative(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        """Each fixed-cost value must be >= 0 (Decimal dict values need a validator)."""
        for job_name, amount in value.items():
            if amount < 0:
                raise ValueError(f"fixed.{job_name} must be >= 0, got {amount}")
        return value

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
            plan_names: list[str] = []
            for p in plans.values():
                if isinstance(p, dict):
                    name = p.get("name")
                    if name is None:
                        raise ConfigError("plan definition is missing required 'name' field")
                else:
                    name = getattr(p, "name", None)
                    if name is None:
                        raise ConfigError("plan definition is missing required 'name' field")
                plan_names.append(name)
            if len(plan_names) != len(set(plan_names)):
                raise ConfigError("duplicate plan names in pricing config")
        cls._validate_tiers(data.get("tiers"))
        return data

    @staticmethod
    def _validate_tiers(tiers: Any) -> None:
        """Validate the optional ``tiers`` section (reject ambiguous/conflicting config).

        - An explicit empty ``tiers: {}`` is ambiguous (omit the key entirely
          for "no tiers").
        - At most one tier may set ``allow_overdraft=True``.
        - At most one tier may set ``is_default=True``.
        - ``default_ttl_days``, when set, must be ``> 0``.
        """
        if tiers is None:
            return
        if not isinstance(tiers, dict):
            raise ConfigError("tiers must be a dict")
        if len(tiers) == 0:
            raise ConfigError("tiers must not be an empty dict — omit the 'tiers' key entirely for no tiers")

        overdraft_count = 0
        default_count = 0
        for t in tiers.values():
            if isinstance(t, dict):
                allow_overdraft = t.get("allow_overdraft", False)
                is_default = t.get("is_default", False)
                ttl = t.get("default_ttl_days")
            else:
                allow_overdraft = getattr(t, "allow_overdraft", False)
                is_default = getattr(t, "is_default", False)
                ttl = getattr(t, "default_ttl_days", None)
            overdraft_count += bool(allow_overdraft)
            default_count += bool(is_default)
            if ttl is not None and ttl <= 0:
                raise ConfigError("tier default_ttl_days must be > 0 when set")
        if overdraft_count > 1:
            raise ConfigError("at most one tier may set allow_overdraft=True")
        if default_count > 1:
            raise ConfigError("at most one tier may set is_default=True")

    @model_validator(mode="after")
    def validate_expressions(self) -> "PricingConfig":
        """Validate all expression strings in the config.

        Variable names are checked against the canonical metric set
        (``METRIC_VARIABLES``) so a typo'd variable fails here, at config-load
        time, rather than at first runtime evaluation (M5).
        """
        known = set(METRIC_VARIABLES)

        for model_name, expr in self.models.items():
            try:
                validate_expression(expr, known_variables=known)
            except ExpressionError as e:
                raise ConfigError(f"invalid expression in models.{model_name}: {e}") from e

        # ``this_tool_calls`` is valid ONLY inside tools expressions (WS2) — it is
        # NOT part of the global METRIC_VARIABLES set used by models/search/cache.
        tools_known = known | {"this_tool_calls"}
        for tool_name, expr in self.tools.items():
            try:
                validate_expression(expr, known_variables=tools_known)
            except ExpressionError as e:
                raise ConfigError(f"invalid expression in tools.{tool_name}: {e}") from e

        for section_name, section_expr in [
            ("search", self.search),
            ("cache", self.cache),
        ]:
            if section_expr is None:
                continue
            try:
                validate_expression(section_expr, known_variables=known)
            except ExpressionError as e:
                raise ConfigError(f"invalid expression in {section_name}: {e}") from e

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
