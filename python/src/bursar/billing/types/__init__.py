from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict, Field, field_validator, model_validator

from bursar.shared.numbers import NonNegativeSafeInteger, PositiveSafeInteger


class BaseModel(PydanticBaseModel):
    """Strict base for public billing contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


NonEmptyString = Annotated[str, Field(min_length=1)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


def _normalize_instant(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


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
    product_id: NonEmptyString | None = None
    price_id: NonEmptyString | None = None
    variant_id: NonEmptyString | None = None
    lookup_key: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "ProviderRef":
        if not any((self.product_id, self.price_id, self.variant_id, self.lookup_key)):
            raise ValueError("provider reference must include product_id, price_id, variant_id, or lookup_key")
        return self


class BillingCustomerInfo(BaseModel):
    provider_customer_id: NonEmptyString | None = None
    email: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "BillingCustomerInfo":
        if self.provider_customer_id is None and self.email is None:
            raise ValueError("billing customer requires provider_customer_id or email")
        return self


class BillingSubscriptionInfo(BaseModel):
    provider_subscription_id: NonEmptyString
    status: BillingSubscriptionStatus | None = None
    cancel_at_period_end: bool | None = None
    period_start: str | None = None
    period_end: str | None = None
    trial_end: str | None = None
    cancel_at: str | None = None
    ended_at: str | None = None
    refs: ProviderRef | None = None
    interval: Literal["day", "week", "month", "year"] | None = None
    interval_count: PositiveSafeInteger | None = None

    @field_validator("period_start", "period_end", "trial_end", "cancel_at", "ended_at")
    @classmethod
    def normalize_optional_instant(cls, value: str | None) -> str | None:
        return _normalize_instant(value) if value is not None else None


class BillingInvoiceInfo(BaseModel):
    provider_invoice_id: NonEmptyString
    status: Literal["draft", "open", "paid", "void", "uncollectible"]
    amount_paid_minor: NonNegativeSafeInteger
    amount_due_minor: NonNegativeSafeInteger
    currency: CurrencyCode
    period_start: str | None = None
    period_end: str | None = None

    @field_validator("period_start", "period_end")
    @classmethod
    def normalize_optional_instant(cls, value: str | None) -> str | None:
        return _normalize_instant(value) if value is not None else None


class BillingInvoiceRecord(BillingInvoiceInfo):
    """Persisted invoice document returned from account-level billing queries."""

    provider: NonEmptyString


class BillingPaymentInfo(BaseModel):
    provider_payment_id: NonEmptyString
    amount_minor: NonNegativeSafeInteger
    tax_minor: NonNegativeSafeInteger
    currency: CurrencyCode
    refs: ProviderRef | None = None
    purpose: Literal["subscription", "credit_topup"]
    status: Literal["pending", "succeeded", "failed", "canceled"]


class BillingPaymentRecord(BaseModel):
    """Persisted payment state used by billing lifecycle handlers."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    provider_payment_id: str
    provider_invoice_id: str | None = None
    user_id: str
    amount_minor: NonNegativeSafeInteger
    tax_minor: NonNegativeSafeInteger
    currency: str
    purpose: Literal["subscription", "credit_topup"]
    status: Literal["pending", "succeeded", "failed", "canceled"]
    provider_updated_at: str
    metadata: dict[str, Any]


class BillingCreditPostingResult(BaseModel):
    """Result of posting a billing grant or refund to the credit ledger."""

    model_config = ConfigDict(extra="forbid")

    ledger_entry_id: str | None = None
    balance_after: Decimal | None = None
    replayed: bool = Field(strict=True)
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_success_result(self) -> "BillingCreditPostingResult":
        if self.error_code is None and (self.ledger_entry_id is None or self.balance_after is None):
            raise ValueError("successful billing credit posting requires ledger entry and balance")
        return self


class BillingRefundInfo(BaseModel):
    provider_refund_id: NonEmptyString
    provider_payment_id: NonEmptyString
    amount_minor: PositiveSafeInteger
    currency: CurrencyCode
    reason: str | None = None
    status: Literal["pending", "succeeded", "failed", "canceled"]


class BillingDisputeInfo(BaseModel):
    provider_dispute_id: NonEmptyString
    provider_payment_id: NonEmptyString
    status: Literal["needs_response", "under_review", "won", "lost", "closed"]
    reason: str | None = None


class BillingEvent(BaseModel):
    provider: NonEmptyString
    event_id: NonEmptyString
    event_type: BillingEventType
    occurred_at: str

    account_id: NonEmptyString | None = None
    customer: BillingCustomerInfo | None = None
    subscription: BillingSubscriptionInfo | None = None
    invoice: BillingInvoiceInfo | None = None
    payment: BillingPaymentInfo | None = None
    refund: BillingRefundInfo | None = None
    dispute: BillingDisputeInfo | None = None
    metadata: dict[str, Any] | None = None
    raw: Any = None
    billing_event_id: NonEmptyString | None = Field(default=None, exclude=True)

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: str) -> str:
        return _normalize_instant(value)

    @model_validator(mode="after")
    def validate_event_payload(self) -> "BillingEvent":
        event_name = self.event_type.value
        if event_name.startswith("customer.") and self.customer is None:
            raise ValueError(f"{event_name} requires customer data")
        if event_name.startswith("subscription.") and self.subscription is None:
            raise ValueError(f"{event_name} requires subscription data")
        if event_name.startswith("invoice.") and self.invoice is None:
            raise ValueError(f"{event_name} requires invoice data")
        if event_name.startswith("payment.") and self.payment is None:
            raise ValueError(f"{event_name} requires payment data")
        if event_name.startswith("refund.") and self.refund is None:
            raise ValueError(f"{event_name} requires refund data")
        if event_name.startswith("dispute.") and self.dispute is None:
            raise ValueError(f"{event_name} requires dispute data")
        return self


class BillingEventResult(BaseModel):
    handled: bool
    action: str | None = None
    error: str | None = None
    subscription_id: str | None = None


BillingEventHandler = Callable[
    [BillingEvent, str],
    None | Awaitable[None],
]


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
EntitlementMode = Literal["allowance", "cycle_grant"]


class SubscriptionGrant(BaseModel):
    """Provider-neutral grant descriptor matching the JavaScript SDK."""

    model_config = ConfigDict(extra="forbid")

    mode: EntitlementMode | None = None
    credits: Decimal | None = None
    bucket: str | None = None
    replace_prior: bool | None = None


class BillingEventClaim(BaseModel):
    status: Literal["claimed", "duplicate", "busy", "retry"]
    claim_token: str | None = None
    billing_event_id: str | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> "BillingEventClaim":
        if self.status == "claimed" and (self.claim_token is None or self.billing_event_id is None):
            raise ValueError("claimed billing event requires claim_token and billing_event_id")
        if self.status != "claimed" and (self.claim_token is not None or self.billing_event_id is not None):
            raise ValueError("non-claimed billing event cannot include claim identifiers")
        return self


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
    status: BillingSubscriptionStatus
    current_period_start: str | None = None
    current_period_end: str | None = None
    trial_end: str | None = None
    cancel_at: str | None = None
    ended_at: str | None = None
    cancel_at_period_end: bool
    interval: str | None = None
    interval_count: PositiveSafeInteger | None = None
    grace_ends_at: str | None = None
    grace_expired_at: str | None = None
    provider_updated_at: str
    metadata: dict[str, Any] | None = None

    @field_validator(
        "current_period_start",
        "current_period_end",
        "trial_end",
        "cancel_at",
        "ended_at",
        "grace_ends_at",
        "grace_expired_at",
        "provider_updated_at",
    )
    @classmethod
    def normalize_timestamps(cls, value: str | None) -> str | None:
        return _normalize_instant(value) if value is not None else None


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

    offer_id: NonEmptyString
    offer_key: NonEmptyString
    plan_id: NonEmptyString
    plan: NonEmptyString
    interval: BillingOfferInterval
    interval_count: PositiveSafeInteger


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
    effective: Literal["immediate", "renewal"]
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
    effective: Literal["immediate", "renewal"]
    idempotency_key: str
    proration_behavior: BillingSubscriptionProrationBehavior = "provider_default"


class BillingGrantResult(BaseModel):
    """Resolved grant info returned by resolve_billing_offer / resolve_billing_offer_by_lookup."""

    mode: Literal["cycle_grant"]
    credits: Decimal = Field(gt=0)
    bucket: str = Field(min_length=1)
    replace_prior: bool


class BillingOfferResult(BaseModel):
    """Typed return type for resolve_billing_offer / resolve_billing_offer_by_lookup."""

    offer_id: str
    offer_key: str
    plan_id: str
    plan: str
    interval: Literal["day", "week", "month", "year"]
    interval_count: PositiveSafeInteger
    grant: BillingGrantResult | None


class BillingTopupResult(BaseModel):
    """Typed return type for resolve_credit_topup / resolve_credit_topup_by_lookup."""

    topup_id: str
    topup_key: str
    credits_per_unit: Decimal = Field(gt=0)
    deposit_to: str = Field(min_length=1)
    amount_minor: NonNegativeSafeInteger
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    min_quantity: PositiveSafeInteger
    max_quantity: PositiveSafeInteger
    default_quantity: PositiveSafeInteger
    min_amount_minor: NonNegativeSafeInteger
    max_amount_minor: NonNegativeSafeInteger


class BillingPreferences(BaseModel):
    """Per-user billing preferences (auto-recharge, notification toggles, overage protection)."""

    user_id: str
    auto_recharge: bool
    overage_protection: bool
    email_notifications: bool
    usage_alerts: bool
    invoice_reminders: bool


AUTO_RECHARGE_STATES = ("disabled", "active", "paused")
BillingAutoRechargeState = Literal["disabled", "active", "paused"]
BillingAutoRechargeAttemptState = Literal[
    "claimed",
    "submitted",
    "processing",
    "unknown",
    "succeeded",
    "failed",
    "action_required",
]


class BillingAutoRechargeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    enabled: bool
    state: BillingAutoRechargeState
    armed: bool = True
    provider: str | None
    topup_id: str | None
    quantity: PositiveSafeInteger
    threshold: Decimal
    max_charges_per_window: PositiveSafeInteger | None
    window_unit: Literal["second", "minute", "hour", "day", "week", "month", "year"]
    window_count: PositiveSafeInteger
    window_anchor: Literal["calendar", "rolling"]
    window_timezone: str
    updated_at: str | None = None


class BillingAutoRechargeAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    provider: str
    idempotency_key: str
    provider_attempt_id: str | None
    topup_id: str
    quantity: PositiveSafeInteger
    state: BillingAutoRechargeAttemptState
    window_start: str
    window_end: str
    quoted_amount_minor: NonNegativeSafeInteger | None
    currency: str | None
    failure_code: str | None
    failure_message: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class BillingAutoRechargeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    state: BillingAutoRechargeState
    threshold_credits: Decimal
    topup_key: str
    quantity: PositiveSafeInteger
    max_recharges: PositiveSafeInteger
    window_start: str
    window_end: str
    recharges_in_window: NonNegativeSafeInteger
    payment_method_id: str | None
    payment_method_last4: str | None
    payment_method_brand: str | None
    suspended_reason: str | None
    pending_attempt_id: str | None
    quote_amount_minor: NonNegativeSafeInteger | None
    quote_currency: str | None


class BillingCustomerRecord(BaseModel):
    """Reverse-lookup result: provider + provider_customer_id for a user."""

    provider: str
    provider_customer_id: str
