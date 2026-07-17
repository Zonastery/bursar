from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bursar.billing.billing_service import BillingServiceImpl
from bursar.billing.models import (
    BillingCustomerRecord,
    BillingEvent,
    BillingEventResult,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionState,
    BillingTopupResult,
    CheckoutIntent,
)
from bursar.billing.store import BillingStore
from bursar.credits_service import CreditsService as CreditsServiceImpl
from bursar.events import CreditEventEmitter
from bursar.interface.base import CreditStore


class BillingEventSink(Protocol):
    """Facade boundary consumed by payment providers."""

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult: ...


class BillingService(BillingEventSink, Protocol):
    """Public billing capability exposed by the Bursar facade."""

    def get_user_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_active_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_blocking_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_user_preferences(self, user_id: str) -> BillingPreferences | None: ...

    def update_user_preferences(self, prefs: BillingPreferences) -> None: ...

    def get_customer_by_user_id(self, user_id: str, provider: str | None = None) -> BillingCustomerRecord | None: ...

    def resolve_offer(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingOfferResult | None: ...

    def resolve_topup(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingTopupResult | None: ...

    def upsert_customer(
        self, provider: str, provider_customer_id: str, user_id: str, email: str | None = None
    ) -> None: ...

    def create_or_get_checkout_intent(
        self,
        actor_key: str,
        provider: str,
        type: str,
        product_id: str,
        request_fingerprint: str,
        expires_at: str,
    ) -> CheckoutIntent: ...

    def update_checkout_intent(
        self,
        id: str,
        status: str | None = None,
        provider_session_id: str | None = None,
        checkout_url: str | None = None,
    ) -> None: ...

    def record_subscription_conflict(
        self,
        user_id: str | None = None,
        provider: str = "",
        duplicate_subscription_id: str = "",
        existing_subscription_id: str | None = None,
        event_id: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...


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

    def setup(self):
        """Run the core database setup migrations."""
        return self.credits.setup()

    def load_catalog(self) -> None:
        """Load the active catalog into the metering engine."""
        self.credits.load_pricing_from_store()

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        """Submit a normalized provider event through the facade."""
        if self.billing is None:
            raise RuntimeError("Bursar billing capability is not configured")
        return self.billing.ingest_billing_event(event)
