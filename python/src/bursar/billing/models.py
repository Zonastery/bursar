from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

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


class BillingProviderRefs(BaseModel):
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
    refs: BillingProviderRefs | None = None
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
    refs: BillingProviderRefs | None = None
    purpose: Literal["subscription", "credit_topup", "unknown"] = "unknown"


class BillingRefundInfo(BaseModel):
    provider_refund_id: str
    provider_payment_id: str | None = None
    amount_minor: int
    currency: str
    reason: str | None = None


class BillingEvent(BaseModel):
    provider: str
    event_id: str
    event_type: str
    occurred_at: str

    user_id: str | None = None
    customer: BillingCustomerInfo | None = None
    subscription: BillingSubscriptionInfo | None = None
    invoice: BillingInvoiceInfo | None = None
    payment: BillingPaymentInfo | None = None
    refund: BillingRefundInfo | None = None
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


class BillingOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_key: str
    plan_key: str
    interval: BillingOfferInterval = BillingOfferInterval.month
    interval_count: int = Field(default=1, ge=1)
    entitlement_mode: Literal["allowance", "cycle_grant"] = "allowance"
    cycle_grant_credits: int | None = None
    cycle_grant_tier: str | None = None
    cycle_grant_replace_prior: bool = True
    provider_refs: dict[str, BillingSubscriptionOfferRef] = Field(default_factory=dict)


class BillingSubscriptionOfferRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    product_id: str | None = None
    price_id: str | None = None
    variant_id: str | None = None
    lookup_key: str | None = None


class BillingCreditTopup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str = "purchased"
    currency: str = "USD"
    credits_per_major_unit: int = 1000
    min_amount_minor: int = 500
    max_amount_minor: int = 500000
    tax_behavior: Literal["exclude_tax", "include_tax"] = "exclude_tax"
    provider_refs: dict[str, BillingSubscriptionOfferRef] = Field(default_factory=dict)


class BillingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscriptions: dict[str, BillingOffer] = Field(default_factory=dict)
    credit_topups: dict[str, BillingCreditTopup] = Field(default_factory=dict)


class BillingEventClaim(BaseModel):
    status: Literal["claimed", "duplicate", "retry"]


class BillingSubscriptionState(BaseModel):
    user_id: str
    provider: str
    provider_subscription_id: str
    provider_customer_id: str | None = None
    offer_key: str | None = None
    plan_key: str | None = None
    status: str = "incomplete"
    current_period_start: str | None = None
    current_period_end: str | None = None
    cancel_at_period_end: bool = False
    interval: str | None = None
    interval_count: int | None = None
    metadata: dict[str, Any] | None = None
