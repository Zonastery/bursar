"""Authoritative Bursar configuration contract — Pydantic model types.

Mirrors the JS SDK's ``config/types.ts`` (the bulk of the configuration
type definitions). Semantic checks that JSON Schema cannot express
(references, units, and expression variables) live here.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from bursar.errors import ConfigError as ConfigError

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
REGION_RE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def _parse_decimal_string(value: Any) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise PydanticCustomError("decimal_string", "must be a base-10 decimal string")
    return Decimal(value)


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal_string),
    PlainSerializer(lambda value: format(value, "f"), return_type=str),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_RE.pattern, "examples": ["10.500000"]}),
]
FeatureValue = bool | int | str


def _validate_identifier(value: str, path: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise PydanticCustomError(
            "invalid_identifier",
            "{path} must be a non-empty snake_case identifier",
            {"path": path},
        )


def _validate_map_keys(values: dict[str, Any], path: str) -> None:
    for key in values:
        _validate_identifier(key, f"{path}.{key}")


def _validate_aware_datetime(value: datetime, path: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PydanticCustomError("timezone_required", "{path} must include an RFC3339 timezone offset", {"path": path})
    return value


def _validate_timezone(value: str, path: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise PydanticCustomError("invalid_timezone", "{path} must be a valid IANA timezone", {"path": path}) from exc
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Duration(StrictModel):
    unit: Literal["second", "minute", "hour", "day", "week"]
    count: int = Field(ge=1)


class BillingInterval(StrictModel):
    unit: Literal["day", "week", "month", "year"]
    count: int = Field(default=1, ge=1)


class CalendarWindow(StrictModel):
    type: Literal["calendar"]
    unit: Literal["day", "week", "month", "year"]
    count: int = Field(default=1, ge=1)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone(value, "window.timezone")


class RollingWindow(StrictModel):
    type: Literal["rolling"]
    duration: Duration


class PlanAssignmentWindow(StrictModel):
    type: Literal["plan_assignment"]
    interval: BillingInterval
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone(value, "window.timezone")


Window = Annotated[CalendarWindow | RollingWindow | PlanAssignmentWindow, Field(discriminator="type")]


class Availability(StrictModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    regions: list[str] = Field(default_factory=list)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value, f"availability.{info.field_name}")

    @field_validator("regions")
    @classmethod
    def validate_regions(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("availability.regions must not contain duplicates")
        if any(not REGION_RE.fullmatch(value) for value in values):
            raise ValueError("availability.regions must contain uppercase ISO-style region codes")
        return values

    @model_validator(mode="after")
    def validate_range(self) -> Availability:
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("availability.ends_at must be later than starts_at")
        return self


class NeverExpiry(StrictModel):
    type: Literal["never"]


class AfterGrantExpiry(StrictModel):
    type: Literal["after_grant"]
    interval: BillingInterval
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone(value, "expiry.timezone")


class EndOfWindowExpiry(StrictModel):
    type: Literal["end_of_window"]
    window: CalendarWindow | PlanAssignmentWindow


class FixedExpiry(StrictModel):
    type: Literal["fixed_at"]
    at: datetime

    @field_validator("at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "expiry.at")


class SubscriptionEndExpiry(StrictModel):
    type: Literal["subscription_end"]


ExpiryPolicy = Annotated[
    NeverExpiry | AfterGrantExpiry | EndOfWindowExpiry | FixedExpiry | SubscriptionEndExpiry,
    Field(discriminator="type"),
]


class MeasureDefinition(StrictModel):
    unit: str

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str) -> str:
        _validate_identifier(value, "measure.unit")
        return value


class DimensionDefinition(StrictModel):
    type: Literal["string", "number", "boolean"]
    required: bool = True


MatcherScalar = str | Decimal | bool


class EqualMatcher(StrictModel):
    op: Literal["eq"]
    value: MatcherScalar


class InMatcher(StrictModel):
    op: Literal["in"]
    values: list[MatcherScalar] = Field(min_length=1)


class NotInMatcher(StrictModel):
    op: Literal["not_in"]
    values: list[MatcherScalar] = Field(min_length=1)


class PrefixMatcher(StrictModel):
    op: Literal["prefix"]
    value: str = Field(min_length=1)


class RangeMatcher(StrictModel):
    op: Literal["range"]
    gt: DecimalValue | None = None
    gte: DecimalValue | None = None
    lt: DecimalValue | None = None
    lte: DecimalValue | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> RangeMatcher:
        if self.gt is None and self.gte is None and self.lt is None and self.lte is None:
            raise ValueError("range matcher requires at least one bound")
        if self.gt is not None and self.gte is not None:
            raise ValueError("range matcher cannot combine gt and gte")
        if self.lt is not None and self.lte is not None:
            raise ValueError("range matcher cannot combine lt and lte")
        lower = self.gt if self.gt is not None else self.gte
        upper = self.lt if self.lt is not None else self.lte
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("range matcher lower bound must be less than upper bound")
        return self


DimensionMatcher = Annotated[
    EqualMatcher | InMatcher | NotInMatcher | PrefixMatcher | RangeMatcher,
    Field(discriminator="op"),
]


class FlatCharge(StrictModel):
    type: Literal["flat"]
    amount: DecimalValue = Field(ge=0)


class PerUnitCharge(StrictModel):
    type: Literal["per_unit"]
    measure: str
    rate: DecimalValue = Field(
        ge=0,
        description="Credits charged for each unit_size units of the selected measure.",
    )
    unit_size: DecimalValue = Field(
        default=Decimal("1"),
        gt=0,
        description="Number of measure units represented by one rate unit; use 1000000 for per-million-token rates.",
    )


class PackageCharge(StrictModel):
    type: Literal["package"]
    measure: str
    units: DecimalValue = Field(gt=0)
    amount: DecimalValue = Field(ge=0)
    rounding: Literal["ceil", "floor", "nearest"] = "ceil"


class GraduatedTier(StrictModel):
    up_to: DecimalValue | None = Field(default=None, gt=0)
    rate: DecimalValue = Field(ge=0)


class GraduatedCharge(StrictModel):
    type: Literal["graduated"]
    measure: str
    tiers: list[GraduatedTier] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tiers(self) -> GraduatedCharge:
        finite = [tier.up_to for tier in self.tiers if tier.up_to is not None]
        if self.tiers[-1].up_to is not None or any(tier.up_to is None for tier in self.tiers[:-1]):
            raise ValueError("graduated tiers must end with exactly one open-ended tier")
        if finite != sorted(finite) or len(finite) != len(set(finite)):
            raise ValueError("graduated tier bounds must be strictly increasing")
        return self


class VolumeCharge(StrictModel):
    type: Literal["volume"]
    measure: str
    tiers: list[GraduatedTier] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tiers(self) -> VolumeCharge:
        GraduatedCharge(type="graduated", measure=self.measure, tiers=self.tiers)
        return self


class ExpressionCharge(StrictModel):
    type: Literal["expression"]
    formula: str = Field(min_length=1)


class SumCharge(StrictModel):
    type: Literal["sum"]
    components: list[Charge] = Field(min_length=1)


Charge = Annotated[
    FlatCharge | PerUnitCharge | PackageCharge | GraduatedCharge | VolumeCharge | ExpressionCharge | SumCharge,
    Field(discriminator="type"),
]


class PriceRule(StrictModel):
    when: dict[str, DimensionMatcher] = Field(min_length=1)
    charge: Charge


class RejectUnmatched(StrictModel):
    action: Literal["reject"]


class ChargeUnmatched(StrictModel):
    action: Literal["charge"]
    charge: Charge


UnmatchedPolicy = Annotated[RejectUnmatched | ChargeUnmatched, Field(discriminator="action")]


class OperationPricing(StrictModel):
    rules: list[PriceRule] = Field(default_factory=list)
    unmatched: UnmatchedPolicy


class OperationDefinition(StrictModel):
    measures: dict[str, MeasureDefinition]
    dimensions: dict[str, DimensionDefinition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_names(self) -> OperationDefinition:
        _validate_map_keys(self.measures, "pricing.operations.*.measures")
        _validate_map_keys(self.dimensions, "pricing.operations.*.dimensions")
        if not self.measures:
            raise ValueError("pricing operations require at least one measure")
        overlap = set(self.measures) & set(self.dimensions)
        if overlap:
            raise ValueError(f"operation reuses names as measures and dimensions: {sorted(overlap)}")
        return self


class RateCard(StrictModel):
    extends: str | None = None
    operations: dict[str, OperationPricing] = Field(default_factory=dict)


class CreditAccounting(StrictModel):
    unit: Literal["credit"] = "credit"
    scale: Literal[6] = 6
    rounding: Literal["half_up"] = "half_up"


class BucketDefinition(StrictModel):
    priority: int = Field(ge=0)
    expiry: ExpiryPolicy = Field(default_factory=lambda: NeverExpiry(type="never"))


class PrepaidCreditPolicy(StrictModel):
    type: Literal["prepaid"]


class CreditLinePolicy(StrictModel):
    type: Literal["credit_line"]
    limit: DecimalValue = Field(gt=0)


CreditPolicy = Annotated[PrepaidCreditPolicy | CreditLinePolicy, Field(discriminator="type")]


class GrantEligibility(StrictModel):
    plans: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)

    @field_validator("regions")
    @classmethod
    def validate_regions(cls, values: list[str]) -> list[str]:
        if any(not REGION_RE.fullmatch(value) for value in values):
            raise ValueError("grant eligibility regions must contain uppercase ISO-style region codes")
        if len(values) != len(set(values)):
            raise ValueError("grant eligibility regions must not contain duplicates")
        return values


class GrantAward(StrictModel):
    recipient: Literal["subject", "referrer"] = "subject"
    amount: DecimalValue = Field(gt=0)
    bucket: str
    expiry: ExpiryPolicy | None = None


class GrantProgram(StrictModel):
    trigger: Literal["account_created", "referral_completed", "promo_code_redeemed", "manual"]
    awards: list[GrantAward] = Field(min_length=1)
    availability: Availability | None = None
    eligibility: GrantEligibility = Field(default_factory=GrantEligibility)
    max_awards_per_subject: int = Field(default=1, ge=1)
    idempotency_scope: Literal["subject", "event"] = "subject"

    @model_validator(mode="after")
    def validate_recipients(self) -> GrantProgram:
        if self.trigger != "referral_completed" and any(award.recipient == "referrer" for award in self.awards):
            raise ValueError("referrer awards require trigger: referral_completed")
        return self


class CreditDisplay(StrictModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    units_per_major: DecimalValue = Field(gt=0)


class CreditsConfig(StrictModel):
    accounting: CreditAccounting = Field(
        default_factory=CreditAccounting,
        description="Fixed v1 accounting convention. It may be omitted; canonical output includes the defaults.",
    )
    buckets: dict[str, BucketDefinition] = Field(default_factory=dict)
    default_bucket: str | None = None
    policies: dict[str, CreditPolicy] = Field(default_factory=dict)
    grant_programs: dict[str, GrantProgram] = Field(default_factory=dict)
    display: CreditDisplay | None = None


class BooleanFeature(StrictModel):
    type: Literal["boolean"]
    default: bool


class EnumFeature(StrictModel):
    type: Literal["enum"]
    values: list[str] = Field(min_length=1)
    default: str

    @model_validator(mode="after")
    def validate_default(self) -> EnumFeature:
        if len(self.values) != len(set(self.values)):
            raise ValueError("enum feature values must be unique")
        if self.default not in self.values:
            raise ValueError("enum feature default must be one of values")
        return self


class IntegerFeature(StrictModel):
    type: Literal["integer"]
    default: int
    minimum: int | None = None
    maximum: int | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> IntegerFeature:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("integer feature minimum cannot exceed maximum")
        if self.minimum is not None and self.default < self.minimum:
            raise ValueError("integer feature default is below minimum")
        if self.maximum is not None and self.default > self.maximum:
            raise ValueError("integer feature default exceeds maximum")
        return self


class StringFeature(StrictModel):
    type: Literal["string"]
    default: str
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value


FeatureDefinition = Annotated[
    BooleanFeature | EnumFeature | IntegerFeature | StringFeature,
    Field(discriminator="type"),
]


class EntitlementsConfig(StrictModel):
    features: dict[str, FeatureDefinition] = Field(default_factory=dict)


class OperationAdmission(StrictModel):
    max_in_flight: int = Field(ge=1)


class AdmissionPolicy(StrictModel):
    max_in_flight: int | None = Field(default=None, ge=1)
    operations: dict[str, OperationAdmission] = Field(default_factory=dict)


class AdmissionConfig(StrictModel):
    policies: dict[str, AdmissionPolicy] = Field(default_factory=dict)


class CreditAllowance(StrictModel):
    amount: DecimalValue = Field(ge=0)
    window: Window


class QuotaDefinition(StrictModel):
    operation: str
    measure: str
    limit: DecimalValue = Field(ge=0)
    window: Window
    enforcement: Literal["block", "allow"]
    emit_at_percent: list[int] = Field(default_factory=lambda: [100])

    @field_validator("emit_at_percent")
    @classmethod
    def validate_thresholds(cls, values: list[int]) -> list[int]:
        if any(value < 1 or value > 100 for value in values):
            raise ValueError("quota event thresholds must be between 1 and 100")
        if values != sorted(set(values)):
            raise ValueError("quota event thresholds must be unique and increasing")
        return values


class PlanDefinition(StrictModel):
    display_name: str = Field(min_length=1)
    rank: int = Field(
        default=0,
        ge=0,
        description="Public catalog ordering. Lower ranks appear first; ties are ordered by plan key.",
    )
    description: str | None = None
    rate_card: str | None = None
    allowed_operations: list[str] = Field(default_factory=list)
    features: dict[str, FeatureValue] = Field(default_factory=dict)
    credit_allowance: CreditAllowance | None = None
    quotas: dict[str, QuotaDefinition] = Field(default_factory=dict)
    credit_policy: str | None = None
    admission_policy: str | None = None
    revision_policy: Literal["immediate", "next_renewal", "pinned"] | None = Field(
        default=None,
        description=(
            "Plan revision behavior. When omitted, subscription-backed plans use next_renewal "
            "and other plans use immediate."
        ),
    )


class StripeProvider(StrictModel):
    type: Literal["stripe"]


class DodoProvider(StrictModel):
    type: Literal["dodo"]


class CustomProvider(StrictModel):
    type: Literal["custom"]
    adapter: str = Field(min_length=1)


ProviderDefinition = Annotated[StripeProvider | DodoProvider | CustomProvider, Field(discriminator="type")]


class StripePriceReference(StrictModel):
    type: Literal["stripe_price"]
    price_id: str = Field(min_length=1)


class DodoProductReference(StrictModel):
    type: Literal["dodo_product"]
    product_id: str = Field(min_length=1)


class CustomObjectReference(StrictModel):
    type: Literal["custom_object"]
    object_kind: Literal["subscription", "one_time"]
    external_id: str = Field(min_length=1)


ProviderReference = Annotated[
    StripePriceReference | DodoProductReference | CustomObjectReference,
    Field(discriminator="type"),
]


class OfferPrice(StrictModel):
    amount_minor: int = Field(ge=0)
    currency: str
    tax_behavior: Literal["inclusive", "exclusive", "unspecified"] = "unspecified"

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if not CURRENCY_RE.fullmatch(value):
            raise ValueError("offer currency must be an uppercase ISO-4217 code")
        return value


class OfferBase(StrictModel):
    display_name: str = Field(min_length=1)
    description: str | None = None
    sort_order: int = 0
    availability: Availability | None = None
    price: OfferPrice
    providers: dict[str, ProviderReference] = Field(min_length=1)


class CycleGrant(StrictModel):
    amount: DecimalValue = Field(gt=0)
    bucket: str
    renewal: Literal["replace_previous", "accumulate"]
    expiry: ExpiryPolicy = Field(default_factory=lambda: SubscriptionEndExpiry(type="subscription_end"))


class SubscriptionOffer(OfferBase):
    type: Literal["subscription"]
    plan: str
    billing_interval: BillingInterval
    trial: BillingInterval | None = None
    cycle_grant: CycleGrant | None = None


class QuantityBounds(StrictModel):
    minimum: int = Field(default=1, ge=1)
    maximum: int = Field(default=1, ge=1)
    default: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> QuantityBounds:
        if self.minimum > self.maximum:
            raise ValueError("quantity minimum cannot exceed maximum")
        if self.default < self.minimum or self.default > self.maximum:
            raise ValueError("quantity default must be within minimum and maximum")
        return self


class TopupOffer(OfferBase):
    type: Literal["topup"]
    credits_per_unit: DecimalValue = Field(gt=0)
    quantity: QuantityBounds = Field(default_factory=QuantityBounds)
    bucket: str
    expiry: ExpiryPolicy | None = None
    lot_behavior: Literal["separate_lots", "merge_and_refresh"] = "separate_lots"


CommerceOffer = Annotated[SubscriptionOffer | TopupOffer, Field(discriminator="type")]


class DecimalRange(StrictModel):
    minimum: DecimalValue = Field(ge=0)
    maximum: DecimalValue = Field(ge=0)
    default: DecimalValue = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> DecimalRange:
        if self.minimum > self.maximum or not self.minimum <= self.default <= self.maximum:
            raise ValueError("decimal range requires minimum <= default <= maximum")
        return self


class IntegerRange(StrictModel):
    minimum: int = Field(ge=1)
    maximum: int = Field(ge=1)
    default: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> IntegerRange:
        if self.minimum > self.maximum or not self.minimum <= self.default <= self.maximum:
            raise ValueError("integer range requires minimum <= default <= maximum")
        return self


class AutoRechargeLimits(StrictModel):
    max_purchases: int = Field(ge=1)
    window: CalendarWindow | RollingWindow
    max_charge_minor: int = Field(ge=1)
    cooldown: Duration
    max_consecutive_failures: int = Field(default=3, ge=1)
    failure_action: Literal["pause"] = "pause"


class AutoRechargeGuardrails(StrictModel):
    eligible_topups: list[str] = Field(min_length=1)
    balance_below: DecimalRange
    rearm_above: DecimalValue = Field(
        gt=0,
        description="Balance that rearms auto-recharge; it must exceed balance_below.maximum.",
    )
    quantity: IntegerRange
    limits: AutoRechargeLimits


class SubscriptionChangePolicy(StrictModel):
    effective: Literal["immediate", "renewal"]
    proration: Literal["prorated", "none"]
    payment_failure: Literal["prevent_change", "apply_change"] = "prevent_change"


class SubscriptionChanges(StrictModel):
    upgrade: SubscriptionChangePolicy | None = None
    downgrade: SubscriptionChangePolicy | None = None
    lateral: SubscriptionChangePolicy | None = None
    cadence_change: SubscriptionChangePolicy | None = None


class CommerceConfig(StrictModel):
    providers: dict[str, ProviderDefinition] = Field(default_factory=dict)
    offers: dict[str, CommerceOffer] = Field(default_factory=dict)
    subscription_changes: SubscriptionChanges | None = None
    auto_recharge: AutoRechargeGuardrails | None = None


class PricingConfig(StrictModel):
    operations: dict[str, OperationDefinition]
    rate_cards: dict[str, RateCard]

    @model_validator(mode="after")
    def validate_pricing(self) -> PricingConfig:
        if not self.operations:
            raise ValueError("pricing.operations must not be empty")
        if not self.rate_cards:
            raise ValueError("pricing.rate_cards must not be empty")
        _validate_map_keys(self.operations, "pricing.operations")
        _validate_map_keys(self.rate_cards, "pricing.rate_cards")
        return self

    def resolves_operation(self, card_key: str, operation: str) -> bool:
        card = self.rate_cards[card_key]
        return operation in card.operations or (
            card.extends is not None and self.resolves_operation(card.extends, operation)
        )


class CatalogConfig(StrictModel):
    default_plan: str | None = None


SumCharge.model_rebuild()


class BursarConfig(StrictModel):
    version: Literal[1]
    catalog: CatalogConfig = Field(
        default_factory=CatalogConfig,
        description="Catalog-wide publication settings such as the default signup plan.",
    )
    pricing: PricingConfig | None = Field(
        default=None,
        description="Usage operations, their measures and dimensions, and reusable rate cards.",
    )
    credits: CreditsConfig = Field(
        description="Credit buckets, spending policies, grants, and optional display conversion.",
    )
    entitlements: EntitlementsConfig = Field(
        default_factory=EntitlementsConfig,
        description="Typed feature definitions referenced by plan feature values.",
    )
    admission: AdmissionConfig = Field(
        default_factory=AdmissionConfig,
        description="Reusable global and per-operation concurrency policies.",
    )
    plans: dict[str, PlanDefinition] = Field(
        default_factory=dict,
        description="Product plans keyed by stable snake_case identifiers.",
    )
    commerce: CommerceConfig = Field(
        default_factory=CommerceConfig,
        description="Payment providers, subscription and top-up offers, and purchase guardrails.",
    )
