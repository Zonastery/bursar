from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bursar.billing.models import BillingCreditTopup, BillingOffer
from bursar.expr import ExpressionError, validate_expression
from bursar.interface.models import BucketDefinition, PlanDefinition
from bursar.metrics import METRIC_VARIABLES

DEFAULT_TOOL_EXPR = "calls * 0"


class ConfigError(ValueError):
    """Raised on config parsing or validation failures."""


class MeteringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: dict[str, str]
    tools: dict[str, str] = Field(default_factory=lambda: {"*": DEFAULT_TOOL_EXPR})
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


class SignupGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: int = Field(ge=0)
    bucket: str


class LedgerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_balance: Decimal = Field(default=Decimal(0), ge=0)
    signup_grant: SignupGrant | None = None
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
            for plan_key, p in plans.items():
                if isinstance(p, dict):
                    label = p.get("label")
                    if label is None:
                        raise ConfigError(f"plan '{plan_key}' is missing required 'label' field")
                else:
                    label = getattr(p, "label", None)
                    if label is None:
                        raise ConfigError(f"plan '{plan_key}' is missing required 'label' field")
                plan_labels.append(label)
            if len(plan_labels) != len(set(plan_labels)):
                raise ConfigError("duplicate plan labels in pricing config")

        ledger = data.get("ledger") if isinstance(data.get("ledger"), dict) else None
        cls._validate_buckets(ledger)
        cls._reject_scalar_signup_grant(ledger)
        return data

    @staticmethod
    def _reject_scalar_signup_grant(ledger: dict | None) -> None:
        if not ledger:
            return
        raw = ledger.get("signup_grant")
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ConfigError(
                "ledger.signup_grant must be an object { amount, bucket } — omit the key to disable signup grants"
            )

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
    def validate_grant_bucket_references(self) -> "PricingConfig":
        buckets = self.ledger.buckets
        bucket_keys = set(buckets.keys()) if buckets else set()

        grant = self.ledger.signup_grant
        if grant is not None:
            if not bucket_keys:
                raise ConfigError(
                    "ledger.signup_grant requires ledger.buckets — define the target bucket before enabling signup grants"
                )
            if grant.bucket not in bucket_keys:
                raise ConfigError(
                    f"ledger.signup_grant.bucket references unknown bucket '{grant.bucket}' "
                    f"(must be one of {sorted(bucket_keys)})"
                )
            target = buckets[grant.bucket]
            if target.expires and target.ttl_days is None:
                raise ConfigError(
                    f"ledger.buckets.{grant.bucket} expires but has no ttl_days — "
                    "signup grants into expiring buckets require ttl_days"
                )

        billing = self.billing
        if billing is None or not bucket_keys:
            return self

        for offer_key, offer in billing.subscriptions.items():
            cycle = offer.grant
            if cycle.mode == "cycle_grant" and cycle.bucket not in bucket_keys:
                raise ConfigError(
                    f"billing.subscriptions.{offer_key}.grant.bucket references unknown bucket "
                    f"'{cycle.bucket}' (must be one of {sorted(bucket_keys)})"
                )

        for topup_key, topup in billing.topups.items():
            if topup.deposit_to not in bucket_keys:
                raise ConfigError(
                    f"billing.topups.{topup_key}.deposit_to references unknown bucket "
                    f"'{topup.deposit_to}' (must be one of {sorted(bucket_keys)})"
                )

        return self

    @model_validator(mode="after")
    def validate_plan_references(self) -> "PricingConfig":
        billing = self.billing
        if billing is not None and billing.subscriptions:
            plans = self.plans or {}
            for offer_key, offer in billing.subscriptions.items():
                plan_ref = offer.plan
                if plan_ref not in plans:
                    raise ConfigError(f"billing.subscriptions.{offer_key}.plan references unknown plan '{plan_ref}'")
        return self

    @model_validator(mode="after")
    def validate_rate_override_keys(self) -> "PricingConfig":
        if not self.plans:
            return self
        model_keys = set(self.metering.models.keys())
        for plan_id, plan_def in self.plans.items():
            if not plan_def.rate_overrides:
                continue
            for override_key in plan_def.rate_overrides:
                if override_key != "*" and override_key not in model_keys:
                    raise ConfigError(
                        f"plans.{plan_id}.rate_overrides.{override_key} references unknown model "
                        f"(must be one of {sorted(model_keys)} or '*')"
                    )
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
        for plan_id, plan_def in (self.plans or {}).items():
            if plan_def.rate_overrides:
                for model_key, expr in plan_def.rate_overrides.items():
                    self._check_expr(expr, f"plans.{plan_id}.rate_overrides.{model_key}", known)

    @staticmethod
    def _check_expr(expr: str, path: str, known: set[str]) -> None:
        try:
            validate_expression(expr, known_variables=known)
        except ExpressionError as e:
            raise ConfigError(f"invalid expression in {path}: {e}") from e


def load_config_from_dict(data: dict) -> PricingConfig:
    try:
        return PricingConfig.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            if err.get("type") != "value_error":
                continue
            msg = err.get("msg", "")
            if not isinstance(msg, str):
                continue
            if msg.startswith("Value error, "):
                raise ConfigError(msg.removeprefix("Value error, ")) from exc
            # Custom ConfigError raised inside validators is serialized into ctx
            ctx = err.get("ctx") or {}
            if isinstance(ctx.get("error"), ConfigError):
                raise ctx["error"] from exc
        raise


def canonical_pricing_config_dict(data: dict) -> dict:
    """Validate and return a canonical snake_case config dict for persistence."""
    return load_config_from_dict(data).model_dump(mode="json")
