from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from bursar.billing.types import (
    BillingAutoRechargeStatus,
    BillingInvoiceInfo,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionState,
)
from bursar.credits.types import BucketBalance, GetUserPlanResult, LedgerEntry
from bursar.providers.types import (
    ChangePlanPreview,
    PaymentMethodInfo,
    PaymentProvider,
    ProviderResolveUserFn,
    SavedPaymentChargeResult,
    WebhookResult,
)

CommerceCheckoutKind = Literal["subscription", "credit_pack"]
PlanChangeClassification = Literal[
    "unchanged",
    "upgrade",
    "downgrade",
    "lateral",
    "cadence_change",
]


@dataclass(frozen=True, slots=True)
class CommerceProviderFactoryContext:
    event_sink: Any
    identity_resolver: ProviderResolveUserFn | None = None


CommerceProviderFactory = Callable[
    [CommerceProviderFactoryContext],
    PaymentProvider | Awaitable[PaymentProvider],
]


@dataclass(slots=True)
class CommerceOptions:
    providers: dict[str, CommerceProviderFactory]
    default_provider: str | None = None
    checkout_intent_ttl_seconds: int = 24 * 60 * 60
    preference_defaults: dict[str, bool] = field(default_factory=dict)
    identity_resolver: ProviderResolveUserFn | None = None
    logger: Any = None


@dataclass(slots=True)
class CreateCheckoutInput:
    subject_id: str
    offer_key: str
    return_url: str
    cancel_url: str
    operation_key: str
    account_id: str | None = None
    email: str | None = None
    provider: str | None = None
    type: CommerceCheckoutKind | None = None
    quantity: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CreateCheckoutResult:
    intent_id: str
    url: str
    provider: str
    offer_key: str


@dataclass(frozen=True, slots=True)
class CheckoutStatusResult:
    intent_id: str
    status: Literal["pending", "succeeded", "failed", "expired"]


@dataclass(frozen=True, slots=True)
class SubscriptionCommandResult:
    ok: Literal[True] = True
    pending: bool | None = None


@dataclass(slots=True)
class PlanChangePreviewResult:
    unchanged: bool
    classification: PlanChangeClassification
    scheduled: bool
    plan_id: str
    interval: Literal["month", "year"]
    preview: ChangePlanPreview | None = None
    quote_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmPlanChangeResult:
    success: Literal[True]
    plan_id: str
    interval: Literal["month", "year"]
    unchanged: bool | None = None
    pending: bool | None = None
    scheduled: bool | None = None
    effective_at: str | None = None


@dataclass(frozen=True, slots=True)
class BillingDocumentInvoiceRef:
    kind: Literal["provider_invoice"]
    provider: str
    provider_document_id: str
    status: str | None = None
    amount_paid_minor: int | None = None
    amount_due_minor: int | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None


@dataclass(frozen=True, slots=True)
class BillingDocumentLedgerRef:
    kind: Literal["ledger_entry"]
    ledger_entry_id: str
    provider: str | None = None
    provider_document_id: str | None = None
    created_at: str = ""
    entry_type: str = ""
    amount: Decimal = Decimal(0)


BillingDocumentRef = BillingDocumentInvoiceRef | BillingDocumentLedgerRef


@dataclass(frozen=True, slots=True)
class CommerceSectionAvailability:
    payment_methods: bool
    documents: bool
    transactions: bool
    usage: bool
    auto_recharge: bool


@dataclass(frozen=True, slots=True)
class AccountCreditOverview:
    ledger_balance: Decimal
    effective_spendable_balance: Decimal
    lifetime_purchases: Decimal
    allowance_remaining: Decimal
    allowance_limit: Decimal
    allowance_period_start: str | None
    allowance_period_end: str | None
    buckets: list[BucketBalance]


@dataclass(frozen=True, slots=True)
class AccountCommerceOverview:
    account_id: str
    credits: AccountCreditOverview
    entitlement: GetUserPlanResult
    subscription: BillingSubscriptionState | None
    pending_change: BillingSubscriptionChange | None
    preferences: BillingPreferences
    payment_methods: list[PaymentMethodInfo]
    documents: list[BillingDocumentRef]
    provider_invoices: list[BillingInvoiceInfo]
    transactions: list[LedgerEntry]
    usage: list[LedgerEntry]
    auto_recharge: BillingAutoRechargeStatus | None
    availability: CommerceSectionAvailability


@dataclass(frozen=True, slots=True)
class AutoRechargeInput:
    account_id: str
    return_url: str | None = None


@dataclass(frozen=True, slots=True)
class CommerceWebhookResult:
    received: bool
    retryable: bool
    provider: str
    event_id: str | None
    event_type: str | None

    @classmethod
    def from_provider(cls, result: WebhookResult) -> CommerceWebhookResult:
        return cls(
            received=result.received,
            retryable=result.retryable,
            provider=result.provider,
            event_id=result.event_id,
            event_type=result.event_type,
        )


AutoRechargeProcessResultLike = Any | SavedPaymentChargeResult
