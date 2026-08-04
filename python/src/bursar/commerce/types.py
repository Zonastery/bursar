from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from bursar.billing.contracts import BillingEventSink
from bursar.billing.types import (
    BillingAutoRechargeStatus,
    BillingInvoiceInfo,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionState,
)
from bursar.credits.types import BucketBalance, GetUserPlanResult, LedgerEntry, UsageCharge
from bursar.providers.types import (
    ChangePlanPreview,
    PaymentMethodInfo,
    PaymentProvider,
    ResolveUserCallback,
    SavedPaymentChargeResult,
    WebhookResult,
)
from bursar.shared.logger import Logger

CommerceCheckoutKind = Literal["subscription", "credit_pack"]
CommerceCheckoutStatus = Literal["pending", "succeeded", "failed", "expired"]
PlanChangeClassification = Literal[
    "unchanged",
    "upgrade",
    "downgrade",
    "lateral",
    "cadence_change",
]


class _CommerceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class CommerceProviderFactoryContext(_CommerceModel):
    tenant_id: str | None = None
    event_sink: SkipValidation[BillingEventSink]
    identity_resolver: ResolveUserCallback | None = None


CommerceProviderFactory = Callable[
    [CommerceProviderFactoryContext],
    PaymentProvider | Awaitable[PaymentProvider],
]


class CommercePreferenceDefaults(_CommerceModel):
    auto_recharge: bool
    overage_protection: bool
    email_notifications: bool
    usage_alerts: bool
    invoice_reminders: bool


class PreferencePatch(_CommerceModel):
    auto_recharge: bool | None = None
    overage_protection: bool | None = None
    email_notifications: bool | None = None
    usage_alerts: bool | None = None
    invoice_reminders: bool | None = None


class CommerceOptions(_CommerceModel):
    tenant_id: str | None = None
    providers: dict[str, CommerceProviderFactory]
    default_provider: str | None = None
    checkout_intent_ttl_ms: int = 24 * 60 * 60 * 1_000
    preference_defaults: PreferencePatch = Field(default_factory=PreferencePatch)
    identity_resolver: ResolveUserCallback | None = None
    logger: SkipValidation[Logger] | None = None


class CreateCheckoutInput(_CommerceModel):
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
    metadata: dict[str, str] = Field(default_factory=dict)


class CreateCheckoutResult(_CommerceModel):
    intent_id: str
    url: str
    provider: str
    offer_key: str


class CheckoutStatusResult(_CommerceModel):
    intent_id: str
    status: CommerceCheckoutStatus


class SubscriptionCommandResult(_CommerceModel):
    ok: Literal[True]
    pending: bool | None = None


class NormalizedPendingPlanChange(_CommerceModel):
    plan_key: str
    interval: Literal["month", "year"]
    effective_at: str
    scheduled: bool
    provider_operation_id: str | None = None


class AccountSubscriptionSummary(_CommerceModel):
    account_id: str
    plan_key: str | None
    status: str | None
    lifecycle_state: str
    access_state: Literal["entitled", "grace", "blocked", "none"]
    is_current: bool
    is_entitled: bool
    is_blocking_checkout: bool
    is_cancellable: bool
    is_terminal: bool
    subscription: BillingSubscriptionState | None
    pending_change: NormalizedPendingPlanChange | None


class CancelSubscriptionResult(_CommerceModel):
    provider: str
    provider_subscription_id: str
    canceled: bool
    error: str | None = None


class CancelAllSubscriptionsResult(_CommerceModel):
    account_id: str
    canceled_count: int
    subscriptions: list[CancelSubscriptionResult]


class PlanChangePreviewResult(_CommerceModel):
    unchanged: bool
    classification: PlanChangeClassification
    scheduled: bool
    plan_id: str
    interval: Literal["month", "year"]
    preview: ChangePlanPreview | None = None
    quote_fingerprint: str | None = None


class PreviewPlanChangeInput(_CommerceModel):
    account_id: str
    offer_key: str


class ConfirmPlanChangeInput(PreviewPlanChangeInput):
    quote_fingerprint: str
    operation_key: str


class ConfirmPlanChangeResult(_CommerceModel):
    success: Literal[True]
    plan_id: str
    interval: Literal["month", "year"]
    unchanged: bool | None = None
    pending: bool | None = None
    scheduled: bool | None = None
    effective_at: str | None = None


class PortalSessionInput(_CommerceModel):
    account_id: str
    purpose: Literal["billing", "payment-method"] = "billing"
    return_url: str
    cancel_url: str | None = None


class BillingDocumentInvoiceRef(_CommerceModel):
    kind: Literal["provider_invoice"]
    provider: str
    provider_document_id: str
    status: str | None = None
    amount_paid_minor: int | None = None
    amount_due_minor: int | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None


class BillingDocumentLedgerRef(_CommerceModel):
    kind: Literal["ledger_entry"]
    ledger_entry_id: str
    provider: str | None = None
    provider_document_id: str | None = None
    created_at: str
    entry_type: str
    amount: Decimal


BillingDocumentRef = Annotated[
    BillingDocumentInvoiceRef | BillingDocumentLedgerRef,
    Field(discriminator="kind"),
]


class BillingDocumentInvoiceLocator(_CommerceModel):
    kind: Literal["provider_invoice"]
    provider: str
    provider_document_id: str


class BillingDocumentLedgerLocator(_CommerceModel):
    kind: Literal["ledger_entry"]
    ledger_entry_id: str


BillingDocumentLocator = Annotated[
    BillingDocumentInvoiceLocator | BillingDocumentLedgerLocator,
    Field(discriminator="kind"),
]


class CommerceSectionAvailability(_CommerceModel):
    payment_methods: bool
    documents: bool
    provider_invoices: bool
    transactions: bool
    usage: bool
    auto_recharge: bool


class AccountAllowanceOverview(_CommerceModel):
    remaining: Decimal
    limit: Decimal | None
    period_start: str | None
    period_end: str | None


class CreditSpendSource(_CommerceModel):
    type: Literal["allowance", "bucket"]
    key: str
    label: str
    priority: int | None


class AccountCreditOverview(_CommerceModel):
    ledger_balance: Decimal
    effective_spendable_balance: Decimal
    lifetime_purchases: Decimal
    allowance: AccountAllowanceOverview
    buckets: list[BucketBalance]
    buckets_by_key: dict[str, Decimal]
    spend_order: list[CreditSpendSource]
    display: dict[str, str] | None = None


class AccountCommerceOverview(_CommerceModel):
    account_id: str
    credits: AccountCreditOverview
    entitlement: GetUserPlanResult
    subscription_summary: AccountSubscriptionSummary
    subscription: BillingSubscriptionState | None
    pending_change: BillingSubscriptionChange | None
    preferences: BillingPreferences
    payment_methods: list[PaymentMethodInfo]
    documents: list[BillingDocumentRef]
    provider_invoices: list[BillingInvoiceInfo]
    transactions: list[LedgerEntry]
    usage: list[UsageCharge]
    auto_recharge: BillingAutoRechargeStatus | None
    availability: CommerceSectionAvailability


class GetInvoiceLinkInput(_CommerceModel):
    account_id: str
    document: BillingDocumentLocator


class CommerceWebhookInput(_CommerceModel):
    provider: str | None = None
    raw_body: str
    headers: dict[str, str]


class AutoRechargeInput(_CommerceModel):
    account_id: str
    return_url: str | None = None


CommerceWebhookResult = WebhookResult


class AutoRechargeProcessResultLike(_CommerceModel):
    outcome: str
    charge: SavedPaymentChargeResult | None = None
