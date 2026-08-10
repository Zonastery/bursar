from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, field_validator, model_validator

from bursar.billing.contracts import BillingEventSink
from bursar.billing.types import (
    BillingAutoRechargeStatus,
    BillingInvoiceRecord,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
)
from bursar.credits.types import BucketBalance, GetUserPlanResult, LedgerEntry, UsageCharge
from bursar.providers.types import (
    ChangePlanPreview,
    PaymentMethodInfo,
    PaymentProvider,
    ProviderEnvironment,
    WebhookResult,
)
from bursar.shared.idempotency import StableKey
from bursar.shared.logger import Logger
from bursar.shared.numbers import NonNegativeSafeInteger, PositiveSafeInteger, SafeInteger

CommerceCheckoutKind = Literal["subscription", "credit_pack"]
CommerceCheckoutStatus = Literal["pending", "succeeded", "failed", "expired"]
SubscriptionAccessState = Literal["entitled", "grace", "blocked", "none"]
PlanChangeClassification = Literal[
    "unchanged",
    "upgrade",
    "downgrade",
    "lateral",
    "cadence_change",
]
NonEmptyString = Annotated[str, Field(min_length=1)]


class _CommerceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class CommerceProviderFactoryContext(_CommerceModel):
    tenant_id: str | None = None
    provider_environment: ProviderEnvironment
    event_sink: SkipValidation[BillingEventSink]


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


class _CommerceOptionsBase(_CommerceModel):
    providers: dict[str, CommerceProviderFactory] = Field(min_length=1)
    default_provider: NonEmptyString | None = None
    checkout_intent_ttl_ms: PositiveSafeInteger = 24 * 60 * 60 * 1_000
    preference_defaults: PreferencePatch = Field(default_factory=PreferencePatch)
    logger: SkipValidation[Logger] | None = None

    @field_validator("providers")
    @classmethod
    def validate_provider_names(
        cls,
        providers: dict[str, CommerceProviderFactory],
    ) -> dict[str, CommerceProviderFactory]:
        if any(not name.strip() for name in providers):
            raise ValueError("payment provider names must not be empty")
        return providers


class CommerceOptions(_CommerceOptionsBase):
    provider_environment: ProviderEnvironment
    tenant_id: NonEmptyString | None = None


class CommerceRuntimeOptions(_CommerceOptionsBase):
    """Commerce options whose tenant and provider environment come from the runtime."""


class CreateCheckoutInput(_CommerceModel):
    subject_id: NonEmptyString
    account_id: NonEmptyString
    offer_key: NonEmptyString
    return_url: NonEmptyString
    cancel_url: NonEmptyString
    operation_key: StableKey
    email: NonEmptyString | None = None
    provider: NonEmptyString | None = None
    type: CommerceCheckoutKind | None = None
    quantity: PositiveSafeInteger | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, metadata: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() for key in metadata):
            raise ValueError("checkout metadata keys must not be empty")
        return metadata


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
    status: BillingSubscriptionStatus | None
    lifecycle_state: BillingSubscriptionStatus | Literal["none"]
    access_state: SubscriptionAccessState
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
    canceled_count: NonNegativeSafeInteger
    subscriptions: list[CancelSubscriptionResult]


class PlanChangePreviewResult(_CommerceModel):
    unchanged: bool
    classification: PlanChangeClassification
    scheduled: bool
    plan_id: str
    interval: Literal["month", "year"]
    preview: ChangePlanPreview | None = None
    quote_fingerprint: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> PlanChangePreviewResult:
        if self.unchanged:
            if (
                self.classification != "unchanged"
                or self.scheduled
                or self.preview is not None
                or self.quote_fingerprint is not None
            ):
                raise ValueError("unchanged plan previews cannot include a provider quote")
        elif self.classification == "unchanged" or self.preview is None or not self.quote_fingerprint:
            raise ValueError("changed plan previews require a classification, preview, and quote fingerprint")
        return self


class PreviewPlanChangeInput(_CommerceModel):
    account_id: NonEmptyString
    offer_key: NonEmptyString


class ConfirmPlanChangeInput(PreviewPlanChangeInput):
    quote_fingerprint: NonEmptyString
    operation_key: StableKey


class ConfirmPlanChangeResult(_CommerceModel):
    success: Literal[True]
    plan_id: str
    interval: Literal["month", "year"]
    unchanged: bool | None = None
    pending: bool | None = None
    scheduled: bool | None = None
    effective_at: str | None = None


class PortalSessionInput(_CommerceModel):
    account_id: NonEmptyString
    purpose: Literal["billing", "payment-method"] = "billing"
    return_url: NonEmptyString
    cancel_url: NonEmptyString | None = None


class BillingDocumentInvoiceRef(_CommerceModel):
    kind: Literal["provider_invoice"]
    provider: str
    provider_document_id: str
    status: str | None = None
    amount_paid_minor: NonNegativeSafeInteger | None = None
    amount_due_minor: NonNegativeSafeInteger | None = None
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
    provider: NonEmptyString
    provider_document_id: NonEmptyString


class BillingDocumentLedgerLocator(_CommerceModel):
    kind: Literal["ledger_entry"]
    ledger_entry_id: NonEmptyString


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
    priority: SafeInteger


class AccountCreditDisplay(_CommerceModel):
    currency: NonEmptyString
    units_per_major: Decimal = Field(gt=0)


class AccountCreditOverview(_CommerceModel):
    ledger_balance: Decimal
    effective_spendable_balance: Decimal
    lifetime_purchases: Decimal
    allowance: AccountAllowanceOverview
    buckets: list[BucketBalance]
    buckets_by_key: dict[str, Decimal]
    spend_order: list[CreditSpendSource]
    display: AccountCreditDisplay | None = None


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
    provider_invoices: list[BillingInvoiceRecord]
    transactions: list[LedgerEntry]
    usage: list[UsageCharge]
    auto_recharge: BillingAutoRechargeStatus | None
    availability: CommerceSectionAvailability


class GetInvoiceLinkInput(_CommerceModel):
    account_id: NonEmptyString
    document: BillingDocumentLocator


class CommerceWebhookInput(_CommerceModel):
    provider: NonEmptyString | None = None
    raw_body: str
    headers: dict[str, str]


class AutoRechargeInput(_CommerceModel):
    account_id: NonEmptyString
    return_url: NonEmptyString | None = None


CommerceWebhookResult = WebhookResult
