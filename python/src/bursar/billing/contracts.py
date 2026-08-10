"""Typed billing boundary contracts mirroring ``billing/contracts.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bursar.billing.types import (
    BillingAutoRechargeAttemptState,
    BillingEvent,
    BillingEventResult,
    BillingSubscriptionChangeState,
)
from bursar.shared.numbers import NonNegativeSafeInteger, PositiveSafeInteger


class _BillingContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckoutIntentCreate(_BillingContract):
    subject_id: str
    provider: str
    checkout_kind: Literal["subscription", "credit_topup"]
    product_key: str
    request_digest: str
    expires_at: str


class CheckoutIntentUpdate(_BillingContract):
    status: Literal["open", "completed", "failed", "expired"] | None = None
    provider_session_id: str | None = None
    checkout_url: str | None = None


class BillingSubscriptionChangeUpdate(_BillingContract):
    state: BillingSubscriptionChangeState | None = None
    provider_operation_id: str | None = None
    error_message: str | None = None


class BillingSubscriptionConflictCreate(_BillingContract):
    user_id: str | None = None
    provider: str
    duplicate_subscription_id: str
    existing_subscription_id: str | None = None
    event_id: str | None = None
    metadata: dict[str, Any] | None = None


class BillingPaymentUpsert(_BillingContract):
    provider: str
    provider_payment_id: str
    provider_invoice_id: str | None = None
    user_id: str
    amount_minor: NonNegativeSafeInteger
    tax_minor: NonNegativeSafeInteger
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    purpose: Literal["subscription", "credit_topup"]
    status: Literal["pending", "succeeded", "failed", "canceled"]
    provider_updated_at: str
    metadata: dict[str, Any] | None = None


class BillingCreditGrantCreate(_BillingContract):
    payment_id: str | None = None
    subscription_id: str | None = None
    topup_id: str | None = None
    configured_credits: Decimal
    quantity: PositiveSafeInteger
    billing_event_id: str | None = None


class BillingRefundUpsert(_BillingContract):
    provider: str
    provider_refund_id: str
    provider_payment_id: str
    user_id: str
    amount_minor: PositiveSafeInteger
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str | None = None
    status: Literal["pending", "succeeded", "failed", "canceled"]
    provider_updated_at: str
    metadata: dict[str, Any] | None = None


class BillingInvoiceUpsert(_BillingContract):
    provider: str
    provider_invoice_id: str
    provider_subscription_id: str | None = None
    user_id: str
    status: Literal["draft", "open", "paid", "void", "uncollectible"]
    amount_paid_minor: NonNegativeSafeInteger
    amount_due_minor: NonNegativeSafeInteger
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period_start: str | None = None
    period_end: str | None = None
    metadata: dict[str, Any] | None = None
    provider_updated_at: str


class BillingDisputeUpsert(_BillingContract):
    provider: str
    provider_dispute_id: str
    provider_payment_id: str
    status: Literal["needs_response", "under_review", "won", "lost", "closed"]
    reason: str | None = None
    metadata: dict[str, Any] | None = None
    provider_updated_at: str


class AutoRechargeAttemptClaim(_BillingContract):
    user_id: str
    idempotency_key: str


class AutoRechargeAttemptUpdate(_BillingContract):
    id: str
    state: BillingAutoRechargeAttemptState
    provider_attempt_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] | None = None


class AutoRechargeProviderPaymentUpdate(_BillingContract):
    provider: str
    provider_payment_id: str
    state: BillingAutoRechargeAttemptState
    failure_code: str | None = None
    failure_message: str | None = None


class BillingEventSink(Protocol):
    """Boundary used by payment providers to submit normalized events."""

    def ingest_billing_event(
        self,
        event: BillingEvent,
    ) -> BillingEventResult: ...


__all__ = [
    "AutoRechargeAttemptClaim",
    "AutoRechargeAttemptUpdate",
    "AutoRechargeProviderPaymentUpdate",
    "BillingCreditGrantCreate",
    "BillingDisputeUpsert",
    "BillingEventSink",
    "BillingInvoiceUpsert",
    "BillingPaymentUpsert",
    "BillingRefundUpsert",
    "BillingSubscriptionChangeUpdate",
    "BillingSubscriptionConflictCreate",
    "CheckoutIntentCreate",
    "CheckoutIntentUpdate",
]
