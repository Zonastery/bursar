from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from bursar.billing.auto_recharge_service import AutoRechargeProcessResult
from bursar.billing.contracts import CheckoutIntentCreate, CheckoutIntentUpdate
from bursar.billing.types import (
    BillingAutoRechargeProfile,
    BillingAutoRechargeStatus,
    BillingCustomerRecord,
    BillingEvent,
    BillingEventResult,
    BillingInvoiceRecord,
    BillingOfferInterval,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionOfferContext,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CheckoutIntent,
    CheckoutIntentStatus,
)
from bursar.commerce import (
    AutoRechargeInput,
    BillingDocumentInvoiceRef,
    BillingDocumentLedgerRef,
    CheckoutCompletedError,
    CheckoutConflictError,
    CommerceOptions,
    CommerceResourceNotFoundError,
    CommerceService,
    CoreBillingDataUnavailableError,
    CreateCheckoutInput,
    InvalidOfferQuantityError,
    PaymentMethodRequiredError,
    ProviderCapabilityNotSupportedError,
    QuoteChangedError,
    UnknownOfferError,
)
from bursar.credits.types import (
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalance,
    BucketBalancesResult,
    GetUserPlanResult,
    LedgerEntry,
    LedgerPage,
    PlanAllowancePolicy,
    UsageChargePage,
)
from bursar.errors import ProviderResponseError
from bursar.providers.types import (
    ChangePlanParams,
    ChangePlanPreview,
    ChangePlanResult,
    CheckoutParams,
    CheckoutPaymentStatus,
    CheckoutSessionResult,
    CheckoutSessionStatus,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    ProviderUrlResult,
    SubscriptionCancellationProvider,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
)

CATALOG = json.loads((Path(__file__).parents[2] / "common" / "commerce-parity.json").read_text())["catalog"]


def commerce_catalog() -> dict[str, Any]:
    catalog = deepcopy(CATALOG)
    catalog["commerce"]["offers"]["pack"] = {
        "type": "topup",
        "display_name": "Credit pack",
        "credits_per_unit": "100",
        "quantity": {"minimum": 2, "maximum": 5, "default": 2},
        "bucket": "general",
        "price": {"amount_minor": 500, "currency": "USD"},
        "providers": {
            "alpha": {
                "type": "custom_object",
                "object_kind": "one_time",
                "external_id": "alpha-pack",
            }
        },
    }
    return catalog


class RecordingProvider:
    provider = "alpha"

    def __init__(self) -> None:
        self.checkout_params: list[CheckoutParams] = []
        self.cancelled: list[tuple[str, str]] = []
        self.reactivated: list[tuple[str, str]] = []
        self.cancelled_changes: list[tuple[str, str | None, str]] = []
        self.change_params: list[ChangePlanParams] = []
        self.preview_params: list[PreviewChangePlanParams] = []
        self.portal_params: list[PortalParams] = []
        self.update_payment_params: list[UpdatePaymentMethodParams] = []
        self.setup_payment_params: list[PaymentMethodSetupParams] = []
        self.invoice_ids: list[str] = []
        self.webhooks: list[WebhookRequest] = []
        self.checkout_status: CheckoutPaymentStatus | None = None
        self.fail_checkout = False
        self.fail_change = False
        self.preview_effective_at = "2026-08-01T00:00:00.000Z"
        self.preview_next_billing_date = "2026-09-01T00:00:00.000Z"

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        self.checkout_params.append(params)
        if self.fail_checkout:
            raise RuntimeError("checkout failed")
        return CheckoutSessionResult(
            url=f"https://checkout.example/{params.product_id}",
            provider_session_id="session-1",
            customer_id="customer-1",
        )

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        self.webhooks.append(req)
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id="event-1",
            event_type="payment.succeeded",
        )

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        if self.checkout_status is None:
            return None
        return CheckoutSessionStatus(payment_status=self.checkout_status)

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        self.cancelled.append((subscription_id, idempotency_key))

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        self.reactivated.append((subscription_id, idempotency_key))

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        *,
        idempotency_key: str,
    ) -> None:
        self.cancelled_changes.append((subscription_id, provider_operation_id, idempotency_key))

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        return [
            PaymentMethodInfo(
                id="pm_1",
                last4="4242",
                brand="visa",
                expiry_month=1,
                expiry_year=2030,
                is_default=True,
            )
        ]

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult:
        self.invoice_ids.append(provider_payment_id)
        return ProviderUrlResult(url=f"https://invoice.example/{provider_payment_id}")

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        self.portal_params.append(params)
        return ProviderUrlResult(url="https://portal.example/session")

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        self.update_payment_params.append(params)
        return ProviderUrlResult(url="https://portal.example/update-payment")

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        self.setup_payment_params.append(params)
        return ProviderUrlResult(url="https://portal.example/setup-payment")

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        self.preview_params.append(params)
        return ChangePlanPreview(
            total_amount=100,
            settlement_amount=100,
            currency="USD",
            line_items=[],
            effective_at=self.preview_effective_at,
            next_billing_date=self.preview_next_billing_date,
        )

    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult:
        self.change_params.append(params)
        if self.fail_change:
            raise RuntimeError("change failed")
        return ChangePlanResult(provider_operation_id="change-1")


class MinimalProvider:
    """Custom provider implementing the same two required capabilities as JS."""

    provider = "minimal"

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        return CheckoutSessionResult(url=params.return_url)

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        del req
        return WebhookResult(
            received=True,
            retryable=False,
            provider="minimal",
            event_id=None,
            event_type=None,
        )


class FakeAutoRecharge:
    def __init__(self) -> None:
        self.status = BillingAutoRechargeStatus(
            enabled=True,
            state="active",
            threshold_credits=Decimal("10"),
            topup_key="pack",
            quantity=2,
            max_recharges=3,
            window_start="2026-07-01T00:00:00Z",
            window_end="2026-08-01T00:00:00Z",
            recharges_in_window=0,
            payment_method_id="pm_1",
            payment_method_last4="4242",
            payment_method_brand="visa",
            suspended_reason=None,
            pending_attempt_id=None,
            quote_amount_minor=500,
            quote_currency="USD",
        )
        self.calls: list[tuple[str, str, Decimal | None, str | None]] = []
        self.fail_payment_method = False
        self.disabled: list[str] = []

    async def get_status(self, user_id: str, provider: PaymentProvider) -> BillingAutoRechargeStatus:
        self.calls.append(("status", user_id, None, provider.provider))
        return self.status

    async def enable(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        balance: Decimal,
        return_url: str | None = None,
    ) -> BillingAutoRechargeStatus:
        self.calls.append(("enable", user_id, balance, return_url))
        if self.fail_payment_method:
            raise PaymentMethodRequiredError
        return self.status

    def disable(self, user_id: str) -> None:
        self.disabled.append(user_id)

    async def retry(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        balance: Decimal,
        return_url: str | None = None,
    ) -> AutoRechargeProcessResult:
        self.calls.append(("retry", user_id, balance, return_url))
        if self.fail_payment_method:
            raise PaymentMethodRequiredError
        return AutoRechargeProcessResult(outcome="submitted")

    async def process_if_needed(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        balance: Decimal,
        return_url: str | None = None,
    ) -> AutoRechargeProcessResult:
        self.calls.append(("process", user_id, balance, return_url))
        return AutoRechargeProcessResult(outcome="submitted")


class FakeBilling:
    def __init__(self) -> None:
        self.catalog = commerce_catalog()
        self.auto_recharge = FakeAutoRecharge()
        self.intent_factory: Any = None
        self.checkout_intent: CheckoutIntent | None = None
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.customers: dict[tuple[str, str | None], BillingCustomerRecord] = {}
        self.upserted_customers: list[tuple[str, str, str, str | None]] = []
        self.subscription: BillingSubscriptionState | None = None
        self.active_subscription: BillingSubscriptionState | None = None
        self.blocking_subscription: BillingSubscriptionState | None = None
        self.cancellable_subscriptions: list[BillingSubscriptionState] = []
        self.events: list[BillingEvent] = []
        self.preferences: BillingPreferences | None = None
        self.saved_preferences: BillingPreferences | None = None
        self.invoices: list[BillingInvoiceRecord] = []
        self.open_change: BillingSubscriptionChange | None = None
        self.created_changes: list[Any] = []
        self.change_updates: list[tuple[str, dict[str, Any]]] = []
        self.auto_recharge_profile: BillingAutoRechargeProfile | None = None
        self.checkout_update_error: Exception | None = None
        self.customer_upsert_error: Exception | None = None

    def get_active_catalog_document(self) -> dict[str, Any]:
        return self.catalog

    def get_blocking_subscription(self, account_id: str | None) -> BillingSubscriptionState | None:
        del account_id
        return self.blocking_subscription

    def get_customer_by_user_id(
        self,
        account_id: str | None,
        provider: str | None = None,
    ) -> BillingCustomerRecord | None:
        if account_id is None:
            return None
        return self.customers.get((account_id, provider)) or self.customers.get((account_id, None))

    def create_or_get_checkout_intent(self, input: CheckoutIntentCreate) -> CheckoutIntent:
        if self.intent_factory is not None:
            return self.intent_factory(input)
        return CheckoutIntent(
            id="intent-1",
            subject_id=input.subject_id,
            provider=input.provider,
            checkout_kind=input.checkout_kind,
            product_key=input.product_key,
            request_digest=input.request_digest,
            status=CheckoutIntentStatus.open,
            expires_at=input.expires_at,
        )

    def update_checkout_intent(
        self,
        intent_id: str,
        update: CheckoutIntentUpdate,
    ) -> None:
        if self.checkout_update_error is not None:
            raise self.checkout_update_error
        self.updates.append((intent_id, update.model_dump(exclude_none=True)))

    def get_checkout_intent(self, intent_id: str, subject_id: str) -> CheckoutIntent | None:
        del subject_id
        if self.checkout_intent and self.checkout_intent.id == intent_id:
            return self.checkout_intent
        return None

    def upsert_customer(self, provider: str, customer_id: str, user_id: str, email: str | None = None) -> None:
        if self.customer_upsert_error is not None:
            raise self.customer_upsert_error
        self.upserted_customers.append((provider, customer_id, user_id, email))

    def get_user_subscription(self, account_id: str) -> BillingSubscriptionState | None:
        del account_id
        return self.subscription

    def get_active_subscription(self, account_id: str) -> BillingSubscriptionState | None:
        del account_id
        return self.active_subscription or self.subscription

    def list_cancellable_subscriptions(self, account_id: str) -> list[BillingSubscriptionState]:
        del account_id
        return self.cancellable_subscriptions

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        self.events.append(event)
        return BillingEventResult(handled=True, action="ok")

    def resolve_offer(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingOfferResult:
        del product_id, price_id
        return BillingOfferResult(
            offer_id="offer-pro",
            offer_key="pro_month",
            plan_id="plan-pro",
            plan="pro",
            interval="month",
            interval_count=1,
            grant=None,
        )

    def resolve_offer_by_lookup(self, provider: str, lookup_key: str) -> BillingOfferResult:
        del provider
        return BillingOfferResult(
            offer_id=f"offer-{lookup_key}",
            offer_key=lookup_key,
            plan_id="plan-pro" if lookup_key == "alpha-pro-month" else "plan-starter",
            plan="pro" if lookup_key == "alpha-pro-month" else "starter",
            interval="year" if lookup_key.endswith("year") else "month",
            interval_count=1,
            grant=None,
        )

    def get_open_billing_subscription_change(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionChange | None:
        del provider, provider_subscription_id
        return self.open_change

    def create_billing_subscription_change(self, value: Any) -> BillingSubscriptionChange:
        self.created_changes.append(value)
        return BillingSubscriptionChange(
            id="change-row",
            subscription_id="subscription-row",
            from_offer_id="offer-starter",
            to_offer_id=value.to_offer_id,
            from_offer=BillingSubscriptionOfferContext(
                offer_id="offer-starter",
                offer_key="starter_month",
                plan_id="plan-starter",
                plan="starter",
                interval=BillingOfferInterval.month,
                interval_count=1,
            ),
            to_offer=BillingSubscriptionOfferContext(
                offer_id=value.to_offer_id,
                offer_key="pro_month",
                plan_id="plan-pro",
                plan="pro",
                interval=BillingOfferInterval.month,
                interval_count=1,
            ),
            effective_at=value.effective_at,
            effective=value.effective,
            state="scheduled" if value.effective == "renewal" else "awaiting_payment",
            proration_behavior=value.proration_behavior,
            idempotency_key=value.idempotency_key,
        )

    def update_billing_subscription_change(self, change_id: str, update: Any) -> None:
        self.change_updates.append((change_id, update.model_dump(exclude_none=True)))

    def get_user_preferences(self, account_id: str) -> BillingPreferences | None:
        del account_id
        return self.preferences

    def update_user_preferences(self, preferences: BillingPreferences) -> None:
        self.saved_preferences = preferences

    def list_billing_invoices(self, account_id: str) -> list[BillingInvoiceRecord]:
        del account_id
        return self.invoices

    def get_auto_recharge_profile(self, account_id: str) -> BillingAutoRechargeProfile | None:
        del account_id
        return self.auto_recharge_profile


class FakeCredits:
    def __init__(self) -> None:
        self.balance = BalanceResult(
            user_id="user-1",
            balance=Decimal("25"),
            lifetime_purchased=Decimal("100"),
        )
        self.available = AvailableResult(
            user_id="user-1",
            balance=Decimal("25"),
            reserved=Decimal("0"),
            available=Decimal("30"),
        )
        self.bucket_balances = BucketBalancesResult(
            user_id="user-1",
            buckets=[
                BucketBalance(
                    bucket_key="general",
                    label="General",
                    priority=10,
                    expires=False,
                    balance=Decimal("30"),
                )
            ],
            total_balance=Decimal("30"),
        )
        self.plan = GetUserPlanResult(
            user_id="user-1",
            plan_id="plan-starter",
            plan_key="starter",
            plan_label="Starter",
            allowance=None,
            entitlements={},
            rate_card=None,
            credit_policy=None,
            admission=None,
            allowed_operations=[],
            plan_assigned_at=None,
            plan_assignment_ends_at=None,
            assignment_source_type=None,
            assignment_source_id=None,
            catalog_revision_pinned=False,
            catalog_version=None,
        )
        self.allowance: AllowanceResult | None = AllowanceResult(
            plan_id="plan-starter",
            allowance_remaining=Decimal("5"),
            period_start="2026-07-01T00:00:00Z",
            period_end="2026-08-01T00:00:00Z",
        )
        self.ledger_entries = LedgerPage(items=[], next_cursor=None)
        self.usage_charges = UsageChargePage(items=[], next_cursor=None)
        self.ledger_entry: LedgerEntry | None = None
        self.fail_balance = False
        self.fail_ledger = False
        self.fail_usage = False
        self.include_record_only: bool | None = None

    def get_balance(self, account_id: str) -> BalanceResult:
        del account_id
        if self.fail_balance:
            raise RuntimeError("ledger unavailable")
        return self.balance

    def get_available(self, account_id: str) -> AvailableResult:
        del account_id
        return self.available

    def get_bucket_balances(self, account_id: str) -> BucketBalancesResult:
        del account_id
        return self.bucket_balances

    def get_user_plan(self, account_id: str) -> GetUserPlanResult:
        del account_id
        return self.plan

    def check_allowance(self, account_id: str) -> AllowanceResult | None:
        del account_id
        return self.allowance

    def list_ledger_entries(self, account_id: str, *, limit: int) -> LedgerPage:
        del account_id, limit
        if self.fail_ledger:
            raise RuntimeError("history unavailable")
        return self.ledger_entries

    def list_usage_charges(
        self,
        account_id: str,
        *,
        limit: int,
        include_record_only: bool = True,
    ) -> UsageChargePage:
        del account_id, limit
        self.include_record_only = include_record_only
        if self.fail_usage:
            raise RuntimeError("usage unavailable")
        return self.usage_charges

    def get_ledger_entry(self, account_id: str, entry_id: str) -> LedgerEntry | None:
        del account_id
        return self.ledger_entry if self.ledger_entry and self.ledger_entry.entry_id == entry_id else None


class Sink:
    def __init__(self) -> None:
        self.events: list[BillingEvent] = []

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        self.events.append(event)
        return BillingEventResult(handled=True, action="ok")


def active_subscription(**overrides: Any) -> BillingSubscriptionState:
    values = {
        "user_id": "user-1",
        "provider": "alpha",
        "provider_subscription_id": "subscription-1",
        "provider_customer_id": "customer-1",
        "plan": "starter",
        "interval": "month",
        "interval_count": 1,
        "status": BillingSubscriptionStatus.active,
        "cancel_at_period_end": False,
        "provider_updated_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return BillingSubscriptionState(**values)


def test_account_summary_validates_subscription_plan_when_entitlements_lag() -> None:
    provider = RecordingProvider()
    commerce, billing, credits, _provider = make_harness(provider)
    billing.subscription = active_subscription()
    credits.plan = credits.plan.model_copy(update={"plan_id": None, "plan_key": None, "plan_label": None})

    summary = commerce.get_account_subscription_summary("user-1")
    assert summary.plan_key == "starter"
    assert summary.is_current is True
    assert summary.is_entitled is False

    billing.subscription = active_subscription(plan="missing")
    with pytest.raises(CoreBillingDataUnavailableError, match="not present in the catalog"):
        commerce.get_account_subscription_summary("user-1")


def scheduled_change() -> BillingSubscriptionChange:
    return BillingSubscriptionChange(
        id="change-existing",
        subscription_id="subscription-row",
        from_offer_id="offer-starter",
        to_offer_id="offer-pro",
        from_offer=BillingSubscriptionOfferContext(
            offer_id="offer-starter",
            offer_key="starter_month",
            plan_id="plan-starter",
            plan="starter",
            interval=BillingOfferInterval.month,
            interval_count=1,
        ),
        to_offer=BillingSubscriptionOfferContext(
            offer_id="offer-pro",
            offer_key="pro_month",
            plan_id="plan-pro",
            plan="pro",
            interval=BillingOfferInterval.month,
            interval_count=1,
        ),
        effective_at="2026-09-01T00:00:00.000Z",
        effective="renewal",
        state="scheduled",
        proration_behavior="none",
        idempotency_key="old-change",
        provider_operation_id="provider-old-change",
    )


def make_harness(
    provider: PaymentProvider | None = None,
) -> tuple[CommerceService, FakeBilling, FakeCredits, PaymentProvider]:
    selected = provider or RecordingProvider()
    billing = FakeBilling()
    credits = FakeCredits()
    providers = {"alpha": lambda _context: selected}
    if selected.provider != "alpha":
        providers[selected.provider] = lambda _context: selected
    service = CommerceService(
        billing,
        credits,
        Sink(),
        CommerceOptions(provider_environment="test", providers=providers),
    )
    return service, billing, credits, selected


def service(provider: RecordingProvider) -> CommerceService:
    return make_harness(provider)[0]


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


@pytest.mark.asyncio
async def test_checkout_resolves_offer_key_before_calling_provider() -> None:
    provider = RecordingProvider()

    result = await service(provider).create_checkout(
        CreateCheckoutInput(
            subject_id="subject-1",
            account_id="account-1",
            offer_key="starter_month",
            return_url="https://app.example/return?intent={intentId}",
            cancel_url="https://app.example/cancel?intent={intentId}",
            operation_key="operation-1",
        )
    )

    assert result.offer_key == "starter_month"
    assert result.provider == "alpha"
    assert result.url == "https://checkout.example/alpha-starter-month"
    assert len(provider.checkout_params) == 1
    assert provider.checkout_params[0].account_id == "account-1"
    assert provider.checkout_params[0].product_id == "alpha-starter-month"
    assert provider.checkout_params[0].return_url.endswith("intent=intent-1")


@pytest.mark.asyncio
async def test_checkout_replay_is_bound_to_the_financial_account() -> None:
    commerce, billing, _credits, _provider = make_harness()
    existing: CheckoutIntent | None = None

    def shared_subject_intent(input: CheckoutIntentCreate) -> CheckoutIntent:
        nonlocal existing
        if existing is None:
            existing = CheckoutIntent(
                id="intent-shared-subject",
                subject_id=input.subject_id,
                provider=input.provider,
                checkout_kind=input.checkout_kind,
                product_key=input.product_key,
                request_digest=input.request_digest,
                status=CheckoutIntentStatus.open,
                expires_at=input.expires_at,
            )
        return existing

    billing.intent_factory = shared_subject_intent
    common = {
        "subject_id": "actor-1",
        "offer_key": "starter_month",
        "return_url": "https://app.example/return",
        "cancel_url": "https://app.example/cancel",
        "operation_key": "actor-checkout",
    }
    await commerce.create_checkout(CreateCheckoutInput.model_validate({**common, "account_id": "account-1"}))

    with pytest.raises(CheckoutConflictError, match="different checkout request"):
        await commerce.create_checkout(CreateCheckoutInput.model_validate({**common, "account_id": "account-2"}))


@pytest.mark.asyncio
async def test_checkout_replay_returns_the_persisted_provider_session() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)

    def persisted_intent(input: CheckoutIntentCreate) -> CheckoutIntent:
        return CheckoutIntent(
            id="intent-replay",
            subject_id=input.subject_id,
            provider=input.provider,
            checkout_kind=input.checkout_kind,
            product_key=input.product_key,
            request_digest=input.request_digest,
            status=CheckoutIntentStatus.open,
            provider_session_id="session-replay",
            checkout_url="https://checkout.example/replay",
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

    billing.intent_factory = persisted_intent
    result = await commerce.create_checkout(
        CreateCheckoutInput(
            subject_id="subject-1",
            account_id="account-1",
            offer_key="pack",
            return_url="https://app.example/return",
            cancel_url="https://app.example/cancel",
            operation_key="checkout-replay",
        )
    )

    assert result.intent_id == "intent-replay"
    assert result.url == "https://checkout.example/replay"
    assert provider.checkout_params == []


@pytest.mark.asyncio
async def test_checkout_persistence_failure_remains_replayable_not_terminal() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.checkout_update_error = RuntimeError("database unavailable")
    checkout = CreateCheckoutInput(
        subject_id="subject-1",
        account_id="account-1",
        offer_key="pack",
        return_url="https://app.example/return",
        cancel_url="https://app.example/cancel",
        operation_key="checkout-persistence-retry",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await commerce.create_checkout(checkout)
    assert billing.updates == []
    assert len(provider.checkout_params) == 1

    billing.checkout_update_error = None
    recovered = await commerce.create_checkout(checkout)
    assert recovered.intent_id == "intent-1"
    assert len(provider.checkout_params) == 2
    assert all(params.idempotency_key == "checkout-persistence-retry" for params in provider.checkout_params)
    assert billing.updates == [
        (
            "intent-1",
            {
                "provider_session_id": "session-1",
                "checkout_url": "https://checkout.example/alpha-pack",
            },
        )
    ]


@pytest.mark.asyncio
async def test_checkout_customer_persistence_failure_remains_replayable_not_terminal() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.customer_upsert_error = RuntimeError("customer database unavailable")
    checkout = CreateCheckoutInput(
        subject_id="subject-1",
        account_id="account-1",
        offer_key="pack",
        return_url="https://app.example/return",
        cancel_url="https://app.example/cancel",
        operation_key="checkout-customer-retry",
    )

    with pytest.raises(RuntimeError, match="customer database unavailable"):
        await commerce.create_checkout(checkout)
    assert billing.updates == []
    assert len(provider.checkout_params) == 1

    billing.customer_upsert_error = None
    recovered = await commerce.create_checkout(checkout)
    assert recovered.intent_id == "intent-1"
    assert len(provider.checkout_params) == 2
    assert all(params.idempotency_key == "checkout-customer-retry" for params in provider.checkout_params)
    assert billing.updates == [
        (
            "intent-1",
            {
                "provider_session_id": "session-1",
                "checkout_url": "https://checkout.example/alpha-pack",
            },
        )
    ]


@pytest.mark.asyncio
async def test_checkout_rejects_unknown_catalog_offer() -> None:
    with pytest.raises(UnknownOfferError):
        await service(RecordingProvider()).create_checkout(
            CreateCheckoutInput(
                subject_id="subject-1",
                account_id="account-1",
                offer_key="alpha-starter-month",
                return_url="https://app.example/return",
                cancel_url="https://app.example/cancel",
                operation_key="operation-1",
            )
        )


@pytest.mark.asyncio
async def test_checkout_enforces_type_quantity_replay_and_failure_state() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)

    with pytest.raises(UnknownOfferError):
        await commerce.create_checkout(
            CreateCheckoutInput(
                subject_id="subject-1",
                account_id="account-1",
                offer_key="pack",
                type="subscription",
                return_url="https://app.example/return",
                cancel_url="https://app.example/cancel",
                operation_key="wrong-type",
            )
        )
    with pytest.raises(InvalidOfferQuantityError):
        await commerce.create_checkout(
            CreateCheckoutInput(
                subject_id="subject-1",
                account_id="account-1",
                offer_key="pack",
                quantity=6,
                return_url="https://app.example/return",
                cancel_url="https://app.example/cancel",
                operation_key="bad-qty",
            )
        )

    provider.checkout_status = "succeeded"

    def completed_intent(input: CheckoutIntentCreate) -> CheckoutIntent:
        return CheckoutIntent(
            id="intent-completed",
            subject_id=input.subject_id,
            provider=input.provider,
            checkout_kind=input.checkout_kind,
            product_key=input.product_key,
            request_digest=input.request_digest,
            status=CheckoutIntentStatus.open,
            provider_session_id="session-existing",
            checkout_url="https://checkout.example/existing",
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

    billing.intent_factory = completed_intent
    with pytest.raises(CheckoutCompletedError):
        await commerce.create_checkout(
            CreateCheckoutInput(
                subject_id="subject-1",
                account_id="account-1",
                offer_key="pack",
                return_url="https://app.example/return",
                cancel_url="https://app.example/cancel",
                operation_key="completed",
            )
        )
    assert billing.updates[-1] == ("intent-completed", {"status": "completed"})

    provider.fail_checkout = True
    billing.intent_factory = None
    updates_before_failure = list(billing.updates)
    with pytest.raises(RuntimeError, match="checkout failed"):
        await commerce.create_checkout(
            CreateCheckoutInput(
                subject_id="subject-1",
                account_id="account-1",
                offer_key="pack",
                return_url="https://app.example/return",
                cancel_url="https://app.example/cancel",
                operation_key="provider-fails",
            )
        )
    assert billing.updates == updates_before_failure


def test_checkout_status_maps_terminal_pending_expired_and_missing() -> None:
    commerce, billing, _credits, _provider = make_harness()
    billing.checkout_intent = CheckoutIntent(
        id="intent-open",
        subject_id="subject-1",
        provider="alpha",
        checkout_kind="credit_topup",
        product_key="pack",
        request_digest="digest",
        status=CheckoutIntentStatus.open,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    assert commerce.get_checkout_status("intent-open", "subject-1").status == "pending"
    billing.checkout_intent = billing.checkout_intent.model_copy(update={"status": CheckoutIntentStatus.completed})
    assert commerce.get_checkout_status("intent-open", "subject-1").status == "succeeded"
    billing.checkout_intent = billing.checkout_intent.model_copy(update={"status": CheckoutIntentStatus.failed})
    assert commerce.get_checkout_status("intent-open", "subject-1").status == "failed"
    billing.checkout_intent = billing.checkout_intent.model_copy(update={"status": CheckoutIntentStatus.expired})
    assert commerce.get_checkout_status("intent-open", "subject-1").status == "expired"
    billing.checkout_intent = billing.checkout_intent.model_copy(
        update={
            "status": CheckoutIntentStatus.open,
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    )
    assert commerce.get_checkout_status("intent-open", "subject-1").status == "expired"
    with pytest.raises(CommerceResourceNotFoundError):
        commerce.get_checkout_status("missing", "subject-1")


@pytest.mark.asyncio
async def test_subscription_commands_and_cancel_all_emit_provider_neutral_events() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.subscription = active_subscription(status=BillingSubscriptionStatus.past_due)

    result = await commerce.cancel_subscription("user-1", "cancel-1")
    assert result.pending is True
    assert provider.cancelled == [("subscription-1", "cancel-1")]
    subscription = billing.events[-1].subscription
    assert subscription is not None
    assert subscription.cancel_at_period_end is True

    billing.subscription = active_subscription(cancel_at_period_end=True)
    result = await commerce.reactivate_subscription("user-1", "reactivate-1")
    assert result.pending is True
    assert provider.reactivated == [("subscription-1", "reactivate-1")]
    subscription = billing.events[-1].subscription
    assert subscription is not None
    assert subscription.cancel_at_period_end is False

    billing.cancellable_subscriptions = [
        active_subscription(provider_subscription_id="subscription-1"),
        active_subscription(provider_subscription_id="subscription-2"),
    ]
    canceled = await commerce.cancel_all_subscriptions("user-1", "cancel-all")
    assert canceled.canceled_count == 2
    assert provider.cancelled[-2:] == [
        ("subscription-1", "cancel-all:cancel-all:5#alpha:14#subscription-1"),
        ("subscription-2", "cancel-all:cancel-all:5#alpha:14#subscription-2"),
    ]
    assert len(billing.events) == 4


@pytest.mark.asyncio
async def test_plan_change_confirmation_requires_explicit_replacement_cancellation_and_restores_failures() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.active_subscription = active_subscription(cancel_at_period_end=True)
    billing.subscription = billing.active_subscription
    billing.open_change = scheduled_change()

    preview = await commerce.preview_plan_change("user-1", offer_key="pro_month")
    assert preview.classification == "upgrade"
    assert preview.quote_fingerprint is not None
    with pytest.raises(CheckoutConflictError, match="Cancel the existing plan change"):
        await commerce.confirm_plan_change(
            "user-1",
            "change-conflicts",
            offer_key="pro_month",
            quote_fingerprint=preview.quote_fingerprint,
        )
    assert provider.cancelled_changes == []
    assert provider.reactivated == []
    assert provider.change_params == []

    billing.open_change = None
    confirmed = await commerce.confirm_plan_change(
        "user-1",
        "change-1",
        offer_key="pro_month",
        quote_fingerprint=preview.quote_fingerprint,
    )
    assert confirmed.pending is True
    assert provider.reactivated == [("subscription-1", "change-1:keep")]
    change_metadata = provider.change_params[0].metadata
    assert change_metadata is not None
    assert change_metadata["plan_slug"] == "pro"
    assert billing.change_updates[-1] == ("change-row", {"provider_operation_id": "change-1"})

    provider.fail_change = True
    preview = await commerce.preview_plan_change("user-1", offer_key="pro_month")
    with pytest.raises(RuntimeError, match="change failed"):
        await commerce.confirm_plan_change(
            "user-1",
            "change-fails",
            offer_key="pro_month",
            quote_fingerprint=preview.quote_fingerprint or "",
        )
    assert billing.change_updates[-1] == (
        "change-row",
        {"state": "failed", "error_message": "subscription_change_failed:RuntimeError"},
    )
    assert provider.cancelled[-1] == ("subscription-1", "change-fails:restore-cancellation")


@pytest.mark.asyncio
async def test_plan_change_quote_mismatch_and_cancel_scheduled_change() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.active_subscription = active_subscription()
    billing.subscription = billing.active_subscription

    with pytest.raises(QuoteChangedError):
        await commerce.confirm_plan_change(
            "user-1",
            "change-mismatch",
            offer_key="pro_month",
            quote_fingerprint="stale",
        )
    assert not provider.change_params

    billing.open_change = scheduled_change()
    assert await commerce.cancel_scheduled_plan_change("user-1", "cancel-scheduled") == {"success": True}
    assert provider.cancelled_changes == [("subscription-1", "provider-old-change", "cancel-scheduled")]
    assert billing.change_updates[-1] == ("change-existing", {"state": "canceled"})


@pytest.mark.asyncio
async def test_plan_change_quote_binds_scheduled_date_but_not_refreshed_immediate_timestamp() -> None:
    provider = RecordingProvider()
    commerce, billing, credits, _provider = make_harness(provider)
    billing.active_subscription = active_subscription()
    billing.subscription = billing.active_subscription

    immediate = await commerce.preview_plan_change("user-1", offer_key="pro_month")
    provider.preview_effective_at = "2026-08-01T00:00:30Z"
    confirmed = await commerce.confirm_plan_change(
        "user-1",
        "change-immediate",
        offer_key="pro_month",
        quote_fingerprint=immediate.quote_fingerprint or "",
    )
    assert confirmed.effective_at == "2026-08-01T00:00:30+00:00"

    billing.active_subscription = active_subscription(plan="pro")
    billing.subscription = billing.active_subscription
    credits.plan = credits.plan.model_copy(update={"plan_key": "pro", "plan_id": "plan-pro"})
    scheduled = await commerce.preview_plan_change("user-1", offer_key="starter_month")
    assert scheduled.scheduled is True
    provider.preview_next_billing_date = "2026-10-01T00:00:00Z"

    with pytest.raises(QuoteChangedError):
        await commerce.confirm_plan_change(
            "user-1",
            "change-scheduled",
            offer_key="starter_month",
            quote_fingerprint=scheduled.quote_fingerprint or "",
        )


@pytest.mark.asyncio
async def test_plan_change_quote_accepts_equivalent_scheduled_instant_offsets() -> None:
    provider = RecordingProvider()
    commerce, billing, credits, _provider = make_harness(provider)
    billing.active_subscription = active_subscription(plan="pro")
    billing.subscription = billing.active_subscription
    credits.plan = credits.plan.model_copy(update={"plan_key": "pro", "plan_id": "plan-pro"})

    scheduled = await commerce.preview_plan_change("user-1", offer_key="starter_month")
    assert scheduled.scheduled is True
    provider.preview_next_billing_date = "2026-09-01T01:00:00+01:00"

    confirmed = await commerce.confirm_plan_change(
        "user-1",
        "change-equivalent-offset",
        offer_key="starter_month",
        quote_fingerprint=scheduled.quote_fingerprint or "",
    )

    assert confirmed.pending is True


@pytest.mark.parametrize(
    "invalid_preview_field",
    [
        {"total_amount": float("nan")},
        {"effective_at": "not-an-instant"},
    ],
    ids=["non-finite-money", "invalid-instant"],
)
@pytest.mark.asyncio
async def test_plan_change_rejects_invalid_custom_provider_preview(
    invalid_preview_field: dict[str, Any],
) -> None:
    class InvalidPreviewProvider(RecordingProvider):
        async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
            self.preview_params.append(params)
            return cast(
                ChangePlanPreview,
                {
                    "total_amount": 100,
                    "settlement_amount": 100,
                    "currency": "usd",
                    "line_items": [],
                    "effective_at": "2026-08-01T00:00:00Z",
                    **invalid_preview_field,
                },
            )

    commerce, billing, _credits, _provider = make_harness(InvalidPreviewProvider())
    billing.active_subscription = active_subscription()
    billing.subscription = billing.active_subscription

    with pytest.raises(ProviderResponseError):
        await commerce.preview_plan_change("user-1", offer_key="pro_month")


@pytest.mark.asyncio
async def test_portal_session_variants_and_optional_capability_errors() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.customers[("user-1", None)] = BillingCustomerRecord(provider="alpha", provider_customer_id="customer-1")
    billing.subscription = active_subscription()

    assert (await commerce.create_portal_session("user-1", "https://return")).url.endswith("/session")
    assert (
        await commerce.create_portal_session(
            "user-1",
            "https://return",
            purpose="payment-method",
        )
    ).url.endswith("/update-payment")
    billing.subscription = None
    assert (
        await commerce.create_portal_session(
            "user-1",
            "https://return",
            purpose="payment-method",
            cancel_url="https://cancel",
        )
    ).url.endswith("/setup-payment")

    unsupported, unsupported_billing, _credits, _provider = make_harness(MinimalProvider())
    unsupported_billing.customers[("user-1", None)] = BillingCustomerRecord(
        provider="minimal",
        provider_customer_id="customer-1",
    )
    with pytest.raises(ProviderCapabilityNotSupportedError):
        await unsupported.create_portal_session("user-1", "https://return")


@pytest.mark.asyncio
async def test_overview_documents_payment_methods_preferences_and_optional_failures() -> None:
    provider = RecordingProvider()
    commerce, billing, credits, _provider = make_harness(provider)
    billing.subscription = active_subscription(grace_ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    billing.open_change = scheduled_change()
    billing.customers[("user-1", "alpha")] = BillingCustomerRecord(
        provider="alpha",
        provider_customer_id="customer-1",
    )
    billing.preferences = BillingPreferences(
        user_id="user-1",
        auto_recharge=True,
        overage_protection=True,
        email_notifications=False,
        usage_alerts=True,
        invoice_reminders=True,
    )
    billing.invoices = [
        BillingInvoiceRecord(
            provider="alpha",
            provider_invoice_id="invoice-1",
            status="paid",
            amount_paid_minor=1000,
            amount_due_minor=1000,
            currency="USD",
        )
    ]
    credits.ledger_entries = LedgerPage(
        items=[
            LedgerEntry(
                entry_id="ledger-1",
                account_id="account-1",
                actor_user_id=None,
                amount=Decimal("12"),
                entry_type="purchase",
                operation="credit_topup",
                reference_entry_id=None,
                idempotency_key="ledger-1",
                metadata={"provider": "alpha", "provider_payment_id": "payment-1"},
                created_at="2026-07-29T00:00:00Z",
            )
        ],
        next_cursor=None,
    )

    overview = await commerce.get_account_overview("user-1")
    assert overview.credits.effective_spendable_balance == Decimal("35")
    assert [(source.type, source.key) for source in overview.credits.spend_order] == [("bucket", "general")]
    assert credits.include_record_only is False
    pending_change = overview.subscription_summary.pending_change
    assert pending_change is not None
    assert pending_change.plan_key == "pro"
    assert overview.payment_methods[0].last4 == "4242"
    assert {document.kind for document in overview.documents} == {"provider_invoice", "ledger_entry"}
    assert overview.availability.payment_methods is True

    credits.fail_ledger = True
    credits.fail_usage = True
    billing.invoices = []
    overview = await commerce.get_account_overview("user-1")
    assert overview.availability.transactions is False
    assert overview.availability.usage is False
    assert overview.availability.documents is False

    credits.fail_balance = True
    with pytest.raises(CoreBillingDataUnavailableError):
        await commerce.get_account_overview("user-1")


@pytest.mark.asyncio
async def test_account_overview_treats_missing_allowance_window_as_zero() -> None:
    commerce, _billing, credits, _provider = make_harness()
    credits.allowance = None

    overview = await commerce.get_account_overview("user-1")

    assert overview.credits.effective_spendable_balance == Decimal("30")
    assert overview.credits.allowance.remaining == Decimal(0)
    assert overview.credits.allowance.period_start is None
    assert overview.credits.allowance.period_end is None


@pytest.mark.asyncio
async def test_account_overview_interleaves_allowance_with_bucket_priorities() -> None:
    commerce, _billing, credits, _provider = make_harness()
    credits.bucket_balances = BucketBalancesResult(
        user_id="user-1",
        buckets=[
            BucketBalance(
                bucket_key="gifted",
                label="Gifted",
                priority=10,
                expires=True,
                balance=Decimal("10"),
            ),
            BucketBalance(
                bucket_key="purchased",
                label="Purchased",
                priority=30,
                expires=False,
                balance=Decimal("20"),
            ),
        ],
        total_balance=Decimal("30"),
    )
    credits.plan.allowance = PlanAllowancePolicy(
        amount=Decimal("10"),
        priority=20,
        reset_unit="month",
        reset_count=1,
        reset_anchor="calendar",
        reset_timezone="UTC",
    )

    overview = await commerce.get_account_overview("user-1")

    assert [(source.type, source.key) for source in overview.credits.spend_order] == [
        ("bucket", "gifted"),
        ("allowance", "allowance"),
        ("bucket", "purchased"),
    ]


@pytest.mark.asyncio
async def test_invoice_links_are_authorized_for_invoice_and_ledger_documents() -> None:
    provider = RecordingProvider()
    commerce, billing, credits, _provider = make_harness(provider)
    billing.invoices = [
        BillingInvoiceRecord(
            provider="alpha",
            provider_invoice_id="invoice-1",
            status="paid",
            amount_paid_minor=0,
            amount_due_minor=0,
            currency="USD",
        )
    ]
    assert (
        await commerce.get_invoice_link(
            "user-1",
            BillingDocumentInvoiceRef(
                kind="provider_invoice",
                provider="alpha",
                provider_document_id="invoice-1",
            ),
        )
    ).url.endswith("/invoice-1")

    credits.ledger_entry = LedgerEntry(
        entry_id="ledger-1",
        account_id="account-1",
        actor_user_id=None,
        amount=Decimal("12"),
        entry_type="purchase",
        operation="credit_topup",
        reference_entry_id=None,
        idempotency_key="ledger-1",
        metadata={"provider": "alpha", "provider_document_id": "document-1"},
        created_at="2026-07-29T00:00:00Z",
    )
    assert (
        await commerce.get_invoice_link(
            "user-1",
            BillingDocumentLedgerRef(
                kind="ledger_entry",
                ledger_entry_id="ledger-1",
                provider=None,
                provider_document_id=None,
                created_at="2026-07-29T00:00:00Z",
                entry_type="purchase",
                amount=Decimal("12"),
            ),
        )
    ).url.endswith("/document-1")

    with pytest.raises(CommerceResourceNotFoundError):
        await commerce.get_invoice_link(
            "user-1",
            BillingDocumentInvoiceRef(
                kind="provider_invoice",
                provider="alpha",
                provider_document_id="missing",
            ),
        )


@pytest.mark.asyncio
async def test_preferences_webhook_and_auto_recharge_workflows() -> None:
    provider = RecordingProvider()
    commerce, billing, _credits, _provider = make_harness(provider)
    billing.preferences = BillingPreferences(
        user_id="user-1",
        auto_recharge=True,
        overage_protection=True,
        email_notifications=False,
        usage_alerts=True,
        invoice_reminders=True,
    )
    prefs = commerce.update_preferences("user-1", {"usage_alerts": False})
    assert prefs.usage_alerts is False
    assert billing.saved_preferences == prefs

    webhook = await commerce.handle_webhook(raw_body="{}", headers={"x-test": "1"}, provider="alpha")
    assert webhook.event_id == "event-1"
    assert provider.webhooks[0].headers == {"x-test": "1"}

    billing.customers[("user-1", None)] = BillingCustomerRecord(provider="alpha", provider_customer_id="customer-1")
    enabled = await commerce.auto_recharge.enable(AutoRechargeInput(account_id="user-1", return_url="https://return"))
    assert enabled is not None
    assert enabled.enabled
    retried = await commerce.auto_recharge.retry(AutoRechargeInput(account_id="user-1", return_url="https://return"))
    assert retried is not None
    assert retried.enabled
    commerce.auto_recharge.disable("user-1")
    assert billing.auto_recharge.disabled == ["user-1"]

    billing.auto_recharge.fail_payment_method = True
    with pytest.raises(PaymentMethodRequiredError):
        await commerce.auto_recharge.enable(AutoRechargeInput(account_id="user-1"))
    with pytest.raises(PaymentMethodRequiredError):
        await commerce.auto_recharge.retry(AutoRechargeInput(account_id="user-1"))

    billing.auto_recharge.fail_payment_method = False
    assert (
        await commerce.auto_recharge.process_if_needed(AutoRechargeInput(account_id="user-1"))
    ).outcome == "disabled"
    billing.auto_recharge_profile = BillingAutoRechargeProfile(
        user_id="user-1",
        enabled=True,
        state="active",
        provider="alpha",
        topup_id="topup-1",
        quantity=2,
        threshold=Decimal("10"),
        max_charges_per_window=3,
        window_unit="month",
        window_count=1,
        window_anchor="calendar",
        window_timezone="UTC",
    )
    result = await commerce.auto_recharge.process_if_needed(AutoRechargeInput(account_id="user-1"))
    assert result.outcome == "submitted"


def test_public_commerce_inputs_do_not_expose_provider_product_ids() -> None:
    checkout_fields = set(CreateCheckoutInput.model_fields)
    assert "offer_key" in checkout_fields
    assert "product_id" not in checkout_fields
    assert "product_id" not in inspect.signature(CommerceService.preview_plan_change).parameters
    assert "product_id" not in inspect.signature(CommerceService.confirm_plan_change).parameters


def test_custom_provider_only_requires_the_js_core_contract() -> None:
    provider = MinimalProvider()

    assert isinstance(provider, PaymentProvider)
    assert not isinstance(provider, SubscriptionCancellationProvider)
