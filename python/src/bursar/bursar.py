from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from bursar.billing.auto_recharge_service import AutoRechargeService
from bursar.billing.billing_service import BillingServiceImpl
from bursar.billing.billing_store import BillingStore
from bursar.billing.types import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCustomerRecord,
    BillingEvent,
    BillingEventResult,
    BillingInvoiceInfo,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionChangeInput,
    BillingSubscriptionChangeState,
    BillingSubscriptionState,
    BillingTopupResult,
    CheckoutIntent,
)
from bursar.credits.events import CreditEventEmitter
from bursar.credits.service import CreditsService as CreditsServiceImpl
from bursar.credits.store import CreditStore


class BillingEventSink(Protocol):
    """Facade boundary consumed by payment providers."""

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult: ...


class BillingService(BillingEventSink, Protocol):
    """Public billing capability exposed by the Bursar facade."""

    auto_recharge: AutoRechargeService

    @property
    def has_provisioning(self) -> bool: ...

    def create_or_get_checkout_intent(
        self,
        subject_id: str,
        provider: str,
        checkout_kind: str,
        product_key: str,
        request_digest: str,
        expires_at: str,
    ) -> CheckoutIntent: ...

    def update_checkout_intent(
        self,
        id: str,
        status: str | None = None,
        provider_session_id: str | None = None,
        checkout_url: str | None = None,
    ) -> None: ...

    def get_checkout_intent(self, id: str, subject_id: str) -> CheckoutIntent | None: ...

    def get_user_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_active_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def list_cancellable_provider_subscription_ids(self, user_id: str) -> list[str]: ...

    def list_cancellable_subscriptions(self, user_id: str) -> list[BillingSubscriptionState]: ...

    def get_blocking_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_user_preferences(self, user_id: str) -> BillingPreferences | None: ...

    def get_active_bursar_config(self) -> dict[str, Any] | None: ...

    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceInfo]: ...

    def create_billing_subscription_change(
        self,
        input: BillingSubscriptionChangeInput,
    ) -> BillingSubscriptionChange: ...

    def get_open_billing_subscription_change(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionChange | None: ...

    def update_billing_subscription_change(
        self,
        id: str,
        *,
        state: BillingSubscriptionChangeState | None = None,
        provider_operation_id: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    def record_subscription_conflict(
        self,
        *,
        user_id: str | None,
        provider: str,
        duplicate_subscription_id: str,
        existing_subscription_id: str | None = None,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None: ...

    def update_user_preferences(self, prefs: BillingPreferences) -> None: ...

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None: ...

    def upsert_auto_recharge_profile(self, profile: BillingAutoRechargeProfile) -> None: ...

    def claim_auto_recharge_attempt(
        self,
        user_id: str,
        idempotency_key: str,
    ) -> BillingAutoRechargeAttempt | None: ...

    def update_auto_recharge_attempt(
        self,
        attempt_id: str,
        state: str,
        provider_attempt_id: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def update_auto_recharge_attempt_by_provider_payment(
        self,
        provider: str,
        provider_payment_id: str,
        state: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None: ...

    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime | int | float,
    ) -> int: ...

    def get_customer_by_user_id(self, user_id: str, provider: str | None = None) -> BillingCustomerRecord | None: ...

    def resolve_offer(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingOfferResult | None: ...

    def resolve_topup(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingTopupResult | None: ...

    def resolve_topup_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingTopupResult | None: ...

    def upsert_customer(
        self, provider: str, provider_customer_id: str, user_id: str, email: str | None = None
    ) -> None: ...

    def pseudonymize_financial_subject(self, user_id: str) -> None: ...


CreditsService = CreditsServiceImpl


@dataclass(slots=True)
class CatalogService:
    """Catalog operations; billing never owns configuration writes."""

    credits: CreditsService

    def active(self):
        return self.credits.get_active_pricing()

    def publish_draft(self, config: dict, label: str | None = None) -> str:
        return self.credits.publish_pricing_draft(config, label)

    def activate(self, version: int) -> str:
        return self.credits.activate_pricing(version)

    def publish_and_activate(self, config: dict, label: str | None = None) -> None:
        self.credits.publish_pricing(config, label)


@dataclass(slots=True)
class Bursar:
    """Single application-facing boundary for credit and billing operations.

    The facade owns the lifecycle of both services and prevents application
    code from wiring unrelated credit and billing implementations together.
    Integrations should depend on ``bursar.credits`` and ``bursar.billing``
    rather than constructing lifecycle services independently.
    """

    credits: CreditsService
    catalog: CatalogService
    billing: BillingService | None = None

    @classmethod
    def create(
        cls,
        *,
        credit_store: CreditStore,
        billing_store: BillingStore | None = None,
        credits: CreditsService | None = None,
        credits_options: dict | None = None,
        billing_options: dict | None = None,
        emitter: CreditEventEmitter | None = None,
    ) -> Bursar:
        credits = credits or CreditsServiceImpl(
            store=credit_store,
            emitter=emitter,
            **(credits_options or {}),
        )
        billing = (
            BillingServiceImpl(
                billing_store,
                **{
                    **(billing_options or {}),
                    # The facade owns this dependency; callers cannot replace
                    # it through the generic manager options dictionary.
                    "provisioning": credits,
                },
            )
            if billing_store is not None
            else None
        )
        return cls(credits=credits, billing=billing, catalog=CatalogService(credits))

    def load_catalog(self) -> None:
        """Load the active catalog into the pricing engine."""
        self.credits.load_pricing_from_store()

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        """Submit a normalized provider event through the facade."""
        if self.billing is None:
            raise RuntimeError("Bursar billing capability is not configured")
        return self.billing.ingest_billing_event(event)
