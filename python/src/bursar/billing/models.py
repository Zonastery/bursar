from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    refs: ProviderRef | None = None
    interval: str | None = None
    interval_count: int | None = None


class BillingInvoiceInfo(BaseModel):
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
    purpose: Literal["subscription", "credit_topup", "unknown"] = "unknown"


class BillingRefundInfo(BaseModel):
    provider_refund_id: str
    provider_payment_id: str | None = None
    amount_minor: int
    currency: str
    reason: str | None = None


class BillingDisputeInfo(BaseModel):
    provider_dispute_id: str
    provider_payment_id: str | None = None
    status: str = "needs_response"
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
    credits: int = Field(ge=0)
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


class BillingCreditTopup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_to: str
    credits_per_unit: int = 1000
    min_amount_minor: int = 500
    max_amount_minor: int = 500000
    tax_behavior: Literal["exclude_tax", "include_tax"] = "exclude_tax"
    providers: dict[str, ProviderRef] = Field(default_factory=dict)


class BillingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    subscriptions: dict[str, BillingOffer] = Field(default_factory=dict)
    topups: dict[str, BillingCreditTopup] = Field(default_factory=dict)

    @classmethod
    def from_pricing_config(cls, cfg: Any) -> "BillingConfig":
        billing_data = getattr(cfg, "billing", None)
        if billing_data is None:
            return cls()
        subscriptions = {}
        if billing_data.subscriptions:
            for key, val in billing_data.subscriptions.items():
                if isinstance(val, dict):
                    subscriptions[key] = BillingOffer(**val)
                else:
                    subscriptions[key] = val
        topups = {}
        if billing_data.topups:
            for key, val in billing_data.topups.items():
                if isinstance(val, dict):
                    topups[key] = BillingCreditTopup(**val)
                else:
                    topups[key] = val
        return cls(
            currency=billing_data.currency or "USD",
            subscriptions=subscriptions,
            topups=topups,
        )


class BillingEventClaim(BaseModel):
    status: Literal["claimed", "duplicate", "retry"]


class BillingSubscriptionState(BaseModel):
    user_id: str
    provider: str
    provider_subscription_id: str
    provider_customer_id: str | None = None
    offer_key: str | None = None
    plan: str | None = None
    status: BillingSubscriptionStatus = BillingSubscriptionStatus.incomplete
    current_period_start: str | None = None
    current_period_end: str | None = None
    cancel_at_period_end: bool = False
    interval: str | None = None
    interval_count: int | None = None
    metadata: dict[str, Any] | None = None
    catalog_version: int | None = None
    plan_version_id: str | None = None


class BillingGrantResult(BaseModel):
    """Resolved grant info returned by resolve_billing_offer / resolve_billing_offer_by_lookup."""

    mode: str | None = None
    credits: str | Decimal | None = None
    bucket: str | None = None
    replace_prior: bool = False


class BillingOfferResult(BaseModel):
    """Typed return type for resolve_billing_offer / resolve_billing_offer_by_lookup."""

    offer_key: str
    plan: str | None = None
    interval: str = "month"
    interval_count: int = 1
    grant: BillingGrantResult = Field(default_factory=BillingGrantResult)


class BillingTopupResult(BaseModel):
    """Typed return type for resolve_credit_topup / resolve_credit_topup_by_lookup."""

    topup_key: str
    credits_per_unit: Decimal | int | None = None
    deposit_to: str = "purchased"
    min_amount_minor: int = 500
    max_amount_minor: int = 500000


class BillingPreferences(BaseModel):
    """Per-user billing preferences (auto-recharge, notification toggles, overage protection)."""

    user_id: str
    auto_recharge: bool = False
    overage_protection: bool = True
    email_notifications: bool = True
    usage_alerts: bool = True
    invoice_reminders: bool = False
    usage_limit_alerts: bool = True


class BillingCustomerRecord(BaseModel):
    """Reverse-lookup result: provider + provider_customer_id for a user."""

    provider: str
    provider_customer_id: str
