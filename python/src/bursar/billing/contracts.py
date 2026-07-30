"""Typed billing boundary contracts mirroring ``billing/contracts.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from bursar.billing.types import (
    BillingEvent,
    BillingEventResult,
    BillingSubscriptionChangeState,
)


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
    user_id: str | None = None
    amount_minor: int = 0
    tax_minor: int | None = None
    currency: str | None = None
    purpose: str = "unknown"
    status: Literal["pending", "succeeded", "failed", "canceled"] = "succeeded"
    provider_updated_at: str | None = None
    metadata: dict[str, Any] | None = None


class BillingCreditGrantCreate(_BillingContract):
    payment_id: str | None = None
    subscription_id: str | None = None
    topup_id: str | None = None
    configured_credits: Decimal
    quantity: int = 1
    billing_event_id: str | None = None


class BillingRefundUpsert(_BillingContract):
    provider: str
    provider_refund_id: str
    provider_payment_id: str | None = None
    user_id: str | None = None
    amount_minor: int = 0
    currency: str | None = None
    reason: str | None = None
    status: Literal["pending", "succeeded", "failed", "canceled"] = "pending"
    provider_updated_at: str | None = None
    metadata: dict[str, Any] | None = None


class BillingInvoiceUpsert(_BillingContract):
    provider: str
    provider_invoice_id: str
    provider_subscription_id: str | None = None
    user_id: str | None = None
    status: str | None = None
    amount_paid_minor: int | None = None
    amount_due_minor: int | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    metadata: dict[str, Any] | None = None
    provider_updated_at: str | None = None


class BillingDisputeUpsert(_BillingContract):
    provider: str
    provider_dispute_id: str
    provider_payment_id: str | None = None
    user_id: str | None = None
    status: str = "needs_response"
    reason: str | None = None
    metadata: dict[str, Any] | None = None
    provider_updated_at: str | None = None


class AutoRechargeAttemptClaim(_BillingContract):
    user_id: str
    idempotency_key: str


class AutoRechargeAttemptUpdate(_BillingContract):
    id: str
    state: str
    provider_attempt_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] | None = None


class AutoRechargeProviderPaymentUpdate(_BillingContract):
    provider: str
    provider_payment_id: str
    state: str
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
