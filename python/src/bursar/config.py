from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bursar.billing.models import BillingCreditTopup, BillingOffer
from bursar.expr import ExpressionError, validate_expression
from bursar.interface.models import BucketDefinition, PlanDefinition
from bursar.metrics import METRIC_VARIABLES


class ConfigError(ValueError):
    """Raised on config parsing or validation failures."""


class MeteringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: dict[str, str]
    tools: dict[str, str] = Field(default_factory=lambda: {"*": "calls * 0"})
    search: str | None = None
    cache_discount: str | None = None
    flat_jobs: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("flat_jobs")
    @classmethod
    def validate_flat_jobs_non_negative(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        for job_name, amount in v.items():
            if amount < 0:
                raise ValueError(f"metering.flat_jobs.{job_name} must be >= 0, got {amount}")
        return v


class LedgerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_balance: Decimal = Field(default=Decimal(0), ge=0)
    signup_grant: int = Field(default=50, ge=0)
    buckets: dict[str, BucketDefinition] | None = None


class BillingSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    subscriptions: dict[str, BillingOffer] = Field(default_factory=dict)
    topups: dict[str, BillingCreditTopup] = Field(default_factory=dict)


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    metering: MeteringConfig
    ledger: LedgerConfig = Field(default_factory=lambda: LedgerConfig())
    plans: dict[str, PlanDefinition] | None = None
    billing: BillingSection | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_structure(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        metering = data.get("metering")
        if metering is None:
            raise ConfigError("missing required section: metering")
        if not isinstance(metering, dict):
            raise ConfigError("metering must be a dict")
        models = metering.get("models")
        if not isinstance(models, dict) or len(models) == 0:
            raise ConfigError("metering.models must be a non-empty dict")

        plans = data.get("plans")
        if plans is not None and isinstance(plans, dict):
            plan_labels: list[str] = []
            for _, p in plans.items():
                label = p.get("label") if isinstance(p, dict) else getattr(p, "label", None)
                if label is not None:
                    plan_labels.append(label)
            if len(plan_labels) != len(set(plan_labels)):
                raise ConfigError("duplicate plan labels in pricing config")

        cls._validate_buckets(data.get("ledger") if isinstance(data.get("ledger"), dict) else None)
        return data

    @staticmethod
    def _validate_buckets(ledger: dict | None) -> None:
        buckets = (ledger or {}).get("buckets") if ledger else None
        if buckets is None:
            return
        if not isinstance(buckets, dict):
            raise ConfigError("ledger.buckets must be a dict")
        if len(buckets) == 0:
            raise ConfigError("ledger.buckets must not be an empty dict — omit the key entirely for no buckets")

        default_count = 0
        overdraft_count = 0
        for b in buckets.values():
            if isinstance(b, dict):
                is_default = b.get("default", False)
                allow_overdraft = b.get("allow_overdraft", False)
                ttl = b.get("ttl_days")
            else:
                is_default = getattr(b, "default", False)
                allow_overdraft = getattr(b, "allow_overdraft", False)
                ttl = getattr(b, "ttl_days", None)
            default_count += bool(is_default)
            overdraft_count += bool(allow_overdraft)
            if ttl is not None and ttl <= 0:
                raise ConfigError("ledger.buckets ttl_days must be > 0 when set")
        if default_count > 1:
            raise ConfigError("at most one bucket may set default=True")
        if overdraft_count > 1:
            raise ConfigError("at most one bucket may set allow_overdraft=True")

    @model_validator(mode="after")
    def validate_plan_references(self) -> "PricingConfig":
        billing = self.billing
        if billing is not None and billing.subscriptions:
            plans = self.plans or {}
            for offer_key, offer in billing.subscriptions.items():
                plan_ref = offer.plan
                if plan_ref is not None and plan_ref not in plans:
                    raise ConfigError(f"billing.subscriptions.{offer_key}.plan references unknown plan '{plan_ref}'")
        return self

    @model_validator(mode="after")
    def validate_expressions(self) -> "PricingConfig":
        known = set(METRIC_VARIABLES)
        self._validate_metering_exprs(known)
        self._validate_tool_exprs(known)
        self._validate_metering_sections(known)
        if self.plans:
            self._validate_plan_exprs(known)
        return self

    def _validate_metering_exprs(self, known: set[str]) -> None:
        for model_name, expr in self.metering.models.items():
            self._check_expr(expr, f"metering.models.{model_name}", known)

    def _validate_tool_exprs(self, known: set[str]) -> None:
        tools_known = known | {"calls"}
        for tool_name, expr in self.metering.tools.items():
            self._check_expr(expr, f"metering.tools.{tool_name}", tools_known)

    def _validate_metering_sections(self, known: set[str]) -> None:
        for section_name, section_expr in (
            ("metering.search", self.metering.search),
            ("metering.cache_discount", self.metering.cache_discount),
        ):
            if section_expr is not None:
                self._check_expr(section_expr, section_name, known)

    def _validate_plan_exprs(self, known: set[str]) -> None:
        for plan_key, plan_def in self.plans.items():
            if plan_def.rate_overrides:
                for model_key, expr in plan_def.rate_overrides.items():
                    self._check_expr(expr, f"plans.{plan_key}.rate_overrides.{model_key}", known)

    @staticmethod
    def _check_expr(expr: str, path: str, known: set[str]) -> None:
        try:
            validate_expression(expr, known_variables=known)
        except ExpressionError as e:
            raise ConfigError(f"invalid expression in {path}: {e}") from e


def load_config_from_dict(data: dict) -> PricingConfig:
    return PricingConfig.model_validate(data)
