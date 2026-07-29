from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BillingProvider(StrEnum):
    stripe = "stripe"
    dodo = "dodo"


class BillingEventType(StrEnum):
    customer_created = "customer.created"
    customer_updated = "customer.updated"
    customer_deleted = "customer.deleted"
    checkout_completed = "checkout.completed"
    checkout_expired = "checkout.expired"
    subscription_created = "subscription.created"
    subscription_updated = "subscription.updated"
    subscription_activated = "subscription.activated"
    subscription_renewed = "subscription.renewed"
    subscription_plan_changed = "subscription.plan_changed"
    subscription_cancellation_scheduled = "subscription.cancellation_scheduled"
    subscription_cancellation_unscheduled = "subscription.cancellation_unscheduled"
    subscription_canceled = "subscription.canceled"
    subscription_expired = "subscription.expired"
    subscription_paused = "subscription.paused"
    subscription_resumed = "subscription.resumed"
    subscription_trial_will_end = "subscription.trial_will_end"
    invoice_created = "invoice.created"
    invoice_finalized = "invoice.finalized"
    invoice_finalization_failed = "invoice.finalization_failed"
    invoice_upcoming = "invoice.upcoming"
    invoice_paid = "invoice.paid"
    invoice_payment_failed = "invoice.payment_failed"
    invoice_payment_action_required = "invoice.payment_action_required"
    invoice_voided = "invoice.voided"
    payment_succeeded = "payment.succeeded"
    payment_failed = "payment.failed"
    refund_created = "refund.created"
    refund_updated = "refund.updated"
    refund_failed = "refund.failed"
    dispute_created = "dispute.created"
    dispute_closed = "dispute.closed"
    payment_method_attached = "payment_method.attached"
    payment_method_updated = "payment_method.updated"
    payment_method_detached = "payment_method.detached"


class BillingSubscriptionStatus(StrEnum):
    incomplete = "incomplete"
    incomplete_expired = "incomplete_expired"
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"
    unpaid = "unpaid"
    paused = "paused"
    expired = "expired"


class ProviderRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str | None = None
    price_id: str | None = None
    variant_id: str | None = None
    lookup_key: str | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "ProviderRef":
        if not any((self.product_id, self.price_id, self.variant_id, self.lookup_key)):
            raise ValueError("provider reference must include product_id, price_id, variant_id, or lookup_key")
        return self


class BillingCustomerInfo(BaseModel):
    provider_customer_id: str | None = None
    email: str | None = None


class BillingSubscriptionInfo(BaseModel):
    provider_subscription_id: str
    status: BillingSubscriptionStatus | None = None
    cancel_at_period_end: bool | None = None
    period_start: str | None = None
    period_end: str | None = None
    trial_end: str | None = None
    cancel_at: str | None = None
    ended_at: str | None = None
    refs: ProviderRef | None = None
    interval: str | None = None
    interval_count: int | None = None


class BillingInvoiceInfo(BaseModel):
    provider: str | None = None
    provider_invoice_id: str
    status: str | None = None
    amount_paid_minor: int | None = None
    amount_due_minor: int | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None


class BillingPaymentInfo(BaseModel):
    provider_payment_id: str
    amount_minor: int
    tax_minor: int | None = None
    currency: str
    refs: ProviderRef | None = None
    purpose: Literal["subscription", "credit_topup", "unknown"]
    status: Literal["pending", "succeeded", "failed", "canceled"] | None = None


class BillingRefundInfo(BaseModel):
    provider_refund_id: str
    provider_payment_id: str | None = None
    amount_minor: int
    currency: str
    reason: str | None = None
    status: Literal["pending", "succeeded", "failed", "canceled"] | None = None


class BillingDisputeInfo(BaseModel):
    provider_dispute_id: str
    provider_payment_id: str | None = None
    status: str | None = None
    reason: str | None = None


class BillingEvent(BaseModel):
    provider: str
    event_id: str
    event_type: BillingEventType
    occurred_at: str

    user_id: str | None = None
    customer: BillingCustomerInfo | None = None
    subscription: BillingSubscriptionInfo | None = None
    invoice: BillingInvoiceInfo | None = None
    payment: BillingPaymentInfo | None = None
    refund: BillingRefundInfo | None = None
    dispute: BillingDisputeInfo | None = None
    metadata: dict[str, Any] | None = None
    raw: Any = None
    billing_event_id: str | None = Field(default=None, exclude=True)

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return parsed.astimezone(UTC).isoformat()


class BillingEventResult(BaseModel):
    handled: bool
    action: str | None = None
    error: str | None = None
    subscription_id: str | None = None


class BillingOfferInterval(StrEnum):
    day = "day"
    week = "week"
    month = "month"
    year = "year"


class AllowanceGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["allowance"] = "allowance"


class CycleGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["cycle_grant"] = "cycle_grant"
    credits: Decimal = Field(ge=0)
    bucket: str
    replace_prior: bool = True


Grant = Annotated[AllowanceGrant | CycleGrant, Field(discriminator="mode")]


class BillingOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str
    interval: BillingOfferInterval = BillingOfferInterval.month
    interval_count: int = Field(default=1, ge=1)
    grant: Grant = Field(default_factory=lambda: AllowanceGrant())
    providers: dict[str, ProviderRef] = Field(default_factory=dict)
    valid_from: str | None = None
    valid_to: str | None = None

    @model_validator(mode="after")
    def validate_validity_window(self) -> "BillingOffer":
        if self.valid_from is not None and self.valid_to is not None:
            try:
                valid_from = datetime.fromisoformat(self.valid_from)
                valid_to = datetime.fromisoformat(self.valid_to)
            except ValueError as exc:
                raise ValueError("valid_from and valid_to must be ISO-8601 timestamps") from exc
            if valid_to <= valid_from:
                raise ValueError("valid_to must be later than valid_from")
        return self


class BillingCreditTopup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_to: str
    credits_per_unit: Decimal = Field(default=Decimal("1000"), gt=0)
    min_amount_minor: int = Field(default=500, ge=0)
    max_amount_minor: int = Field(default=500000, ge=0)
    tax_behavior: Literal["exclude_tax", "include_tax"] = "exclude_tax"
    providers: dict[str, ProviderRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_amount_range(self) -> "BillingCreditTopup":
        if self.max_amount_minor < self.min_amount_minor:
            raise ValueError("max_amount_minor must be >= min_amount_minor")
        if not self.credits_per_unit.is_finite():
            raise ValueError("credits_per_unit must be finite")
        return self


class BillingEventClaim(BaseModel):
    status: Literal["claimed", "duplicate", "retry"]
    claim_token: str | None = None
    billing_event_id: str | None = None


class CheckoutIntentStatus(StrEnum):
    open = "open"
    completed = "completed"
    failed = "failed"
    expired = "expired"


class CheckoutIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject_id: str
    provider: str
    checkout_kind: Literal["subscription", "credit_topup"]
    product_key: str
    request_digest: str
    status: CheckoutIntentStatus
    provider_session_id: str | None = None
    checkout_url: str | None = None
    expires_at: str


class BillingSubscriptionState(BaseModel):
    subscription_id: str | None = None
    user_id: str
    provider: str
    provider_subscription_id: str
    provider_customer_id: str | None = None
    offer_key: str | None = None
    offer_id: str | None = None
    plan: str | None = None
    plan_id: str | None = None
    status: BillingSubscriptionStatus = BillingSubscriptionStatus.incomplete
    current_period_start: str | None = None
    current_period_end: str | None = None
    trial_end: str | None = None
    cancel_at: str | None = None
    ended_at: str | None = None
    cancel_at_period_end: bool = False
    interval: str | None = None
    interval_count: int | None = None
    grace_ends_at: str | None = None
    grace_expired_at: str | None = None
    provider_updated_at: str | None = None
    metadata: dict[str, Any] | None = None


BillingSubscriptionChangeState = Literal[
    "awaiting_payment",
    "scheduled",
    "applied",
    "failed",
    "canceled",
]
BillingSubscriptionProrationBehavior = Literal[
    "provider_default",
    "invoice_immediately",
    "none",
]


class BillingSubscriptionOfferContext(BaseModel):
    """Catalog context captured for one side of a subscription change."""

    model_config = ConfigDict(extra="forbid")

    offer_id: str
    offer_key: str
    plan_id: str | None = None
    plan: str | None = None
    interval: str | None = None
    interval_count: int | None = None


class BillingSubscriptionChange(BaseModel):
    """Durable provider-neutral state for a customer-initiated offer change."""

    model_config = ConfigDict(extra="forbid")

    id: str
    subscription_id: str
    from_offer_id: str
    to_offer_id: str
    from_offer: BillingSubscriptionOfferContext
    to_offer: BillingSubscriptionOfferContext
    effective_at: str | None
    state: BillingSubscriptionChangeState
    proration_behavior: BillingSubscriptionProrationBehavior
    idempotency_key: str
    provider_operation_id: str | None = None
    error_message: str | None = None


class BillingSubscriptionChangeInput(BaseModel):
    """Input for opening a subscription offer change."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_subscription_id: str
    to_offer_id: str
    effective_at: str
    idempotency_key: str
    proration_behavior: BillingSubscriptionProrationBehavior = "provider_default"


class BillingGrantResult(BaseModel):
    """Resolved grant info returned by resolve_billing_offer / resolve_billing_offer_by_lookup."""

    mode: str | None = None
    credits: str | Decimal | None = None
    bucket: str | None = None
    replace_prior: bool | None = None


class BillingOfferResult(BaseModel):
    """Typed return type for resolve_billing_offer / resolve_billing_offer_by_lookup."""

    offer_id: str
    offer_key: str
    plan_id: str | None = None
    plan: str | None = None
    interval: str | None = None
    interval_count: int | None = None
    grant: BillingGrantResult | None = None


class BillingTopupResult(BaseModel):
    """Typed return type for resolve_credit_topup / resolve_credit_topup_by_lookup."""

    topup_id: str
    topup_key: str
    credits_per_unit: Decimal | int | None = None
    deposit_to: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    default_quantity: int | None = None
    min_amount_minor: int | None = None
    max_amount_minor: int | None = None


class BillingPreferences(BaseModel):
    """Per-user billing preferences (auto-recharge, notification toggles, overage protection)."""

    user_id: str
    auto_recharge: bool = False
    overage_protection: bool = True
    email_notifications: bool = True
    usage_alerts: bool = True
    invoice_reminders: bool = False


class BillingAutoRechargeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    enabled: bool = False
    state: Literal["disabled", "active", "paused"] = "disabled"
    armed: bool = True
    provider: str | None = None
    topup_id: str | None = None
    quantity: int = 1
    threshold: Decimal = Decimal(0)
    max_charges_per_window: int | None = None
    window_unit: Literal["second", "minute", "hour", "day", "week", "month", "year"] = "month"
    window_count: int = 1
    window_anchor: Literal["calendar", "plan_assignment", "rolling"] = "calendar"
    window_timezone: str = "UTC"
    updated_at: str | None = None


class BillingAutoRechargeAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    provider: str
    idempotency_key: str
    provider_attempt_id: str | None = None
    topup_id: str
    quantity: int
    state: Literal[
        "claimed",
        "submitted",
        "processing",
        "unknown",
        "succeeded",
        "failed",
        "action_required",
    ]
    window_start: str
    window_end: str
    quoted_amount_minor: int | None = None
    currency: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class BillingAutoRechargeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    state: Literal["disabled", "active", "paused"]
    threshold_credits: Decimal
    topup_key: str
    quantity: int
    max_recharges: int
    window_days: float
    window_start: str
    window_end: str
    recharges_in_window: int
    payment_method_id: str | None = None
    payment_method_last4: str | None = None
    payment_method_brand: str | None = None
    suspended_reason: str | None = None
    pending_attempt_id: str | None = None
    quote_amount_minor: int | None = None
    quote_currency: str | None = None


class BillingCustomerRecord(BaseModel):
    """Reverse-lookup result: provider + provider_customer_id for a user."""

    provider: str
    provider_customer_id: str
