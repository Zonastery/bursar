"""The public Bursar pricing configuration contract.

Version ``1`` intentionally has a single, compact vocabulary.  It models
arbitrary billable operations, credit wallets, plans and provider-owned
payment catalog references.  The module is deliberately independent from the
SQL catalogue representation; the latter is an implementation detail.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bursar.expr import ExpressionError, validate_expression

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PeriodUnit = Literal["day", "week", "month", "year"]
PeriodAnchor = Literal["calendar", "plan_assignment", "rolling"]
LimitAction = Literal["deny", "warn", "notify"]
BillingMode = Literal["strict", "overdraft"]


class ConfigError(ValueError):
    """Raised when a Bursar configuration is invalid."""


def _validate_identifier(value: str, path: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{path} must be a non-empty snake_case identifier")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(StrictModel):
    unit: PeriodUnit
    count: int = Field(default=1, ge=1)
    anchor: PeriodAnchor = "calendar"
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("timezone must be non-empty")
        return value


class OperationDefinition(StrictModel):
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)

    @field_validator("measures", "dimensions")
    @classmethod
    def validate_names(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("operation measures and dimensions must not contain duplicates")
        for value in values:
            _validate_identifier(value, "operation measure/dimension")
        return values


class DimensionMatcher(StrictModel):
    exact: str | None = None
    prefix: str | None = None

    @model_validator(mode="after")
    def validate_matcher(self) -> DimensionMatcher:
        if (self.exact is None) == (self.prefix is None):
            raise ValueError("a dimension matcher requires exactly one of exact or prefix")
        if self.exact is not None and not self.exact:
            raise ValueError("dimension matcher exact must be non-empty")
        if self.prefix is not None and not self.prefix:
            raise ValueError("dimension matcher prefix must be non-empty")
        return self


class PriceRule(StrictModel):
    match: dict[str, DimensionMatcher] | None = None
    default: bool = False
    formula: str

    @model_validator(mode="after")
    def validate_shape(self) -> PriceRule:
        if self.default == bool(self.match):
            raise ValueError("a price rule must be either a match rule or the default rule")
        return self


class RateCard(StrictModel):
    extends: str | None = None
    prices: dict[str, list[PriceRule]] = Field(default_factory=dict)


class UsageConfig(StrictModel):
    operations: dict[str, OperationDefinition]
    rate_cards: dict[str, RateCard]

    @model_validator(mode="after")
    def validate_usage(self) -> UsageConfig:  # noqa: C901
        if not self.operations:
            raise ValueError("usage.operations must not be empty")
        if not self.rate_cards:
            raise ValueError("usage.rate_cards must not be empty")
        for operation, definition in self.operations.items():
            _validate_identifier(operation, "usage.operations key")
            overlap = set(definition.measures) & set(definition.dimensions)
            if overlap:
                raise ValueError(
                    f"usage.operations.{operation} reuses names as measures and dimensions: {sorted(overlap)}"
                )
        for card_key, card in self.rate_cards.items():
            _validate_identifier(card_key, "usage.rate_cards key")
            if card.extends is not None and card.extends not in self.rate_cards:
                raise ValueError(f"usage.rate_cards.{card_key}.extends references unknown rate card '{card.extends}'")
            for operation, rules in card.prices.items():
                if operation not in self.operations:
                    raise ValueError(f"usage.rate_cards.{card_key}.prices references unknown operation '{operation}'")
                if not rules:
                    raise ValueError(f"usage.rate_cards.{card_key}.prices.{operation} must not be empty")
                if not rules[-1].default or sum(rule.default for rule in rules) != 1:
                    raise ValueError(
                        f"usage.rate_cards.{card_key}.prices.{operation} must end with exactly one default rule"
                    )
                dimensions = set(self.operations[operation].dimensions)
                measures = set(self.operations[operation].measures)
                for index, rule in enumerate(rules):
                    if rule.match and not set(rule.match).issubset(dimensions):
                        unknown = sorted(set(rule.match) - dimensions)
                        message = f"usage.rate_cards.{card_key}.prices.{operation}[{index}] "
                        message += f"matches undeclared dimensions {unknown}"
                        raise ValueError(message)
                    try:
                        validate_expression(rule.formula, known_variables=measures)
                    except ExpressionError as exc:
                        raise ValueError(
                            f"invalid formula in usage.rate_cards.{card_key}.prices.{operation}[{index}]: {exc}"
                        ) from exc

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError(f"usage.rate_cards inheritance cycle includes '{key}'")
            if key in visited:
                return
            visiting.add(key)
            parent = self.rate_cards[key].extends
            if parent is not None:
                visit(parent)
            visiting.remove(key)
            visited.add(key)

        for key in self.rate_cards:
            visit(key)

        def prices_operation(card_key: str, operation: str) -> bool:
            card = self.rate_cards[card_key]
            return operation in card.prices or (card.extends is not None and prices_operation(card.extends, operation))

        for card_key in self.rate_cards:
            for operation in self.operations:
                if not prices_operation(card_key, operation):
                    raise ValueError(f"usage.rate_cards.{card_key} has no price for operation '{operation}'")
        return self


class BucketDefinition(StrictModel):
    expires_after: Period | None = None


class SignupGrant(StrictModel):
    amount: Decimal = Field(gt=0)
    bucket: str


class CreditsConfig(StrictModel):
    buckets: dict[str, BucketDefinition] = Field(default_factory=dict)
    spend_order: list[str] = Field(default_factory=list)
    default_bucket: str | None = None
    overdraft_bucket: str | None = None
    signup_grant: SignupGrant | None = None

    @model_validator(mode="after")
    def validate_credits(self) -> CreditsConfig:
        if not self.buckets:
            if self.spend_order or self.default_bucket is not None or self.overdraft_bucket is not None:
                raise ValueError("credits bucket settings require credits.buckets")
            if self.signup_grant is not None:
                raise ValueError("credits.signup_grant requires credits.buckets")
            return self
        for key in self.buckets:
            _validate_identifier(key, "credits.buckets key")
        if set(self.spend_order) != set(self.buckets) or len(self.spend_order) != len(self.buckets):
            raise ValueError("credits.spend_order must list every bucket exactly once")
        if self.default_bucket not in self.buckets:
            raise ValueError("credits.default_bucket must reference a configured bucket")
        if self.overdraft_bucket is not None and self.overdraft_bucket not in self.buckets:
            raise ValueError("credits.overdraft_bucket must reference a configured bucket")
        if self.signup_grant is not None and self.signup_grant.bucket not in self.buckets:
            raise ValueError("credits.signup_grant.bucket must reference a configured bucket")
        return self


class IncludedCredits(StrictModel):
    amount: Decimal = Field(ge=0)
    reset: Period


class FeatureLimit(StrictModel):
    max_calls: int = Field(ge=0)
    period: Period
    action: LimitAction = "deny"


class OperationSpendingPolicy(StrictModel):
    max_concurrent: int | None = Field(default=None, gt=0)
    mode: BillingMode | None = None
    overdraft_limit: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_overdraft(self) -> OperationSpendingPolicy:
        if self.overdraft_limit is not None and self.mode not in (None, "overdraft"):
            raise ValueError("overdraft_limit requires overdraft mode")
        return self


class SpendingPolicy(StrictModel):
    mode: BillingMode = "strict"
    overdraft_limit: Decimal | None = Field(default=None, ge=0)
    max_concurrent: int | None = Field(default=None, gt=0)
    operations: dict[str, OperationSpendingPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_overdraft(self) -> SpendingPolicy:
        if self.overdraft_limit is not None and self.mode != "overdraft":
            raise ValueError("plans.*.spending.overdraft_limit requires overdraft mode")
        for key in self.operations:
            _validate_identifier(key, "plans.*.spending.operations key")
        return self


class PlanDefinition(StrictModel):
    display_name: str
    rate_card: str | None = None
    included_credits: IncludedCredits | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, FeatureLimit] = Field(default_factory=dict)
    spending: SpendingPolicy = Field(default_factory=SpendingPolicy)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("plans.*.display_name must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> PlanDefinition:
        for key in self.features:
            _validate_identifier(key, "plans.*.features key")
        for key in self.limits:
            _validate_identifier(key, "plans.*.limits key")
        return self


class ProviderLookup(StrictModel):
    type: str
    value: str

    @field_validator("type", "value")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider lookup type and value must be non-empty")
        return value


class ProviderReference(StrictModel):
    lookup: ProviderLookup


class RenewalCredits(StrictModel):
    amount: Decimal = Field(gt=0)
    bucket: str
    behavior: Literal["replace", "accumulate"]
    on_subscription_end: Literal["expire", "keep"]


class SubscriptionOffer(StrictModel):
    plan: str
    billing_period: Period
    providers: dict[str, ProviderReference]
    renewal_credits: RenewalCredits | None = None
    stack_credits: bool = False


class TopupOffer(StrictModel):
    credits: Decimal = Field(gt=0)
    bucket: str
    providers: dict[str, ProviderReference]


class AutoRechargeTrigger(StrictModel):
    balance_below: Decimal = Field(ge=0)


class AutoRechargePurchase(StrictModel):
    topup: str
    quantity: int = Field(default=1, ge=1)


class AutoRechargeLimit(StrictModel):
    max_purchases: int = Field(ge=1)
    period: Period


class AutoRechargePolicy(StrictModel):
    trigger: AutoRechargeTrigger
    purchase: AutoRechargePurchase
    limit: AutoRechargeLimit


class PaymentsConfig(StrictModel):
    subscriptions: dict[str, SubscriptionOffer] = Field(default_factory=dict)
    topups: dict[str, TopupOffer] = Field(default_factory=dict)
    auto_recharge: AutoRechargePolicy | None = None


class BursarConfig(StrictModel):
    version: Literal[1] = 1
    usage: UsageConfig | None = None
    credits: CreditsConfig = Field(default_factory=CreditsConfig)
    plans: dict[str, PlanDefinition] = Field(default_factory=dict)
    payments: PaymentsConfig | None = None

    @model_validator(mode="after")
    def validate_references(self) -> BursarConfig:  # noqa: C901
        if self.usage is not None:
            for key, plan in self.plans.items():
                _validate_identifier(key, "plans key")
                card = plan.rate_card
                if card is not None and card not in self.usage.rate_cards:
                    raise ValueError(f"plans.{key}.rate_card references unknown rate card '{card}'")
                if plan.spending.mode == "overdraft" and self.credits.overdraft_bucket is None:
                    raise ValueError(f"plans.{key}.spending overdraft mode requires credits.overdraft_bucket")
        elif any(plan.rate_card is not None for plan in self.plans.values()):
            raise ValueError("plans.*.rate_card requires usage")

        payments = self.payments
        if payments is None:
            return self
        seen_refs: set[tuple[str, str, str]] = set()
        for key, offer in payments.subscriptions.items():
            _validate_identifier(key, "payments.subscriptions key")
            if offer.plan not in self.plans:
                raise ValueError(f"payments.subscriptions.{key}.plan references unknown plan '{offer.plan}'")
            self._validate_provider_refs(f"payments.subscriptions.{key}", offer.providers, seen_refs)
            if offer.renewal_credits is not None:
                if offer.renewal_credits.bucket not in self.credits.buckets:
                    raise ValueError(f"payments.subscriptions.{key}.renewal_credits.bucket references unknown bucket")
                if self.plans[offer.plan].included_credits is not None and not offer.stack_credits:
                    raise ValueError(
                        f"payments.subscriptions.{key} combines included_credits and renewal_credits; "
                        "set stack_credits: true"
                    )
        for key, offer in payments.topups.items():
            _validate_identifier(key, "payments.topups key")
            if offer.bucket not in self.credits.buckets:
                raise ValueError(f"payments.topups.{key}.bucket references unknown bucket")
            self._validate_provider_refs(f"payments.topups.{key}", offer.providers, seen_refs)
        if payments.auto_recharge is not None:
            topup = payments.auto_recharge.purchase.topup
            if topup not in payments.topups:
                raise ValueError(f"payments.auto_recharge.purchase.topup references unknown top-up '{topup}'")
        return self

    @staticmethod
    def _validate_provider_refs(path: str, refs: dict[str, ProviderReference], seen: set[tuple[str, str, str]]) -> None:
        if not refs:
            raise ValueError(f"{path}.providers must not be empty")
        for provider, ref in refs.items():
            _validate_identifier(provider, f"{path}.providers key")
            key = (provider, ref.lookup.type, ref.lookup.value)
            if key in seen:
                raise ValueError(f"duplicate provider lookup {provider}/{ref.lookup.type}/{ref.lookup.value}")
            seen.add(key)


def load_config_from_dict(data: dict[str, Any]) -> BursarConfig:
    try:
        return BursarConfig.model_validate(data)
    except ValidationError as exc:
        errors = "; ".join(error.get("msg", "invalid configuration") for error in exc.errors())
        raise ConfigError(errors) from exc


def canonical_bursar_config_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the canonical public v1 config document."""
    return load_config_from_dict(data).model_dump(mode="json", exclude_none=True)
