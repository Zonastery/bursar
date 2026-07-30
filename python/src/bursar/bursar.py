from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, SkipValidation

from bursar.billing.auto_recharge_service import AutoRechargeService
from bursar.billing.billing_service import BillingServiceImpl
from bursar.billing.billing_store import BillingStore
from bursar.billing.contracts import (
    AutoRechargeAttemptClaim,
    AutoRechargeAttemptUpdate,
    AutoRechargeProviderPaymentUpdate,
    BillingEventSink,
    BillingSubscriptionChangeUpdate,
    BillingSubscriptionConflictCreate,
    CheckoutIntentCreate,
    CheckoutIntentUpdate,
)
from bursar.billing.service_types import BillingServiceOptions
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
    BillingSubscriptionState,
    BillingTopupResult,
    CheckoutIntent,
)
from bursar.commerce.service import CommerceService
from bursar.commerce.types import AutoRechargeInput, CommerceOptions
from bursar.credits.events import CreditEventEmitter
from bursar.credits.service import CreditsService as CreditsServiceImpl
from bursar.credits.service_types import CreditsServiceOptions
from bursar.credits.store import CreditStore


class BillingCapability(BillingEventSink, Protocol):
    """Public billing capability exposed by the Bursar facade."""

    auto_recharge: AutoRechargeService

    @property
    def has_provisioning(self) -> bool: ...

    def create_or_get_checkout_intent(
        self,
        input: CheckoutIntentCreate,
    ) -> CheckoutIntent: ...

    def update_checkout_intent(
        self,
        id: str,
        update: CheckoutIntentUpdate,
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
        update: BillingSubscriptionChangeUpdate,
    ) -> None: ...

    def record_subscription_conflict(
        self,
        input: BillingSubscriptionConflictCreate,
    ) -> None: ...

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None: ...

    def update_user_preferences(self, prefs: BillingPreferences) -> None: ...

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None: ...

    def upsert_auto_recharge_profile(self, profile: BillingAutoRechargeProfile) -> None: ...

    def claim_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptClaim,
    ) -> BillingAutoRechargeAttempt | None: ...

    def update_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptUpdate,
    ) -> None: ...

    def update_auto_recharge_attempt_by_provider_payment(
        self,
        input: AutoRechargeProviderPaymentUpdate,
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

    def resolve_offer_by_lookup(
        self,
        provider: str,
        lookup_key: str,
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

    def expire_past_due_grace_periods(
        self,
        now: datetime | None = None,
    ) -> int: ...

    def invalidate_offer_cache(self) -> None: ...


BillingService = BillingCapability
CreditsService = CreditsServiceImpl


class CatalogService:
    """Catalog operations; billing never owns configuration writes."""

    def __init__(self, credits: CreditsService) -> None:
        self._credits = credits

    @property
    def active(self):
        return self._credits.get_active_pricing()

    def publish_draft(self, config: dict, label: str | None = None) -> str:
        return self._credits.publish_pricing_draft(config, label)

    def activate(self, version: int) -> str:
        return self._credits.activate_pricing(version)

    def publish_and_activate(self, config: dict, label: str | None = None) -> None:
        self._credits.publish_pricing(config, label)


class BursarOptions(BaseModel):
    """Typed construction options mirroring the JavaScript ``BursarOptions``."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    credit_store: SkipValidation[CreditStore]
    billing_store: SkipValidation[BillingStore] | None = None
    credits: SkipValidation[CreditsService] | None = None
    credits_options: CreditsServiceOptions | None = None
    billing_options: BillingServiceOptions | None = None
    commerce_options: CommerceOptions | None = None
    emitter: SkipValidation[CreditEventEmitter] | None = None


class Bursar:
    """Single application-facing boundary for credit and billing operations.

    The facade owns the lifecycle of both services and prevents application
    code from wiring unrelated credit and billing implementations together.
    Integrations should depend on ``bursar.credits`` and ``bursar.billing``
    rather than constructing lifecycle services independently.
    """

    credits: CreditsService
    catalog: CatalogService
    billing: BillingService | None
    commerce: CommerceService | None

    def __init__(
        self,
        options: BursarOptions | None = None,
        *,
        credits: CreditsService | None = None,
        catalog: CatalogService | None = None,
        billing: BillingService | None = None,
        commerce: CommerceService | None = None,
    ) -> None:
        if options is None:
            if credits is None:
                raise TypeError("Bursar requires BursarOptions or a credits service")
            self.credits = credits
            self.catalog = catalog or CatalogService(credits)
            self.billing = billing
            self.commerce = commerce
            return

        self.credits = options.credits or CreditsServiceImpl(
            store=options.credit_store,
            emitter=options.emitter,
            options=options.credits_options,
        )
        self.catalog = CatalogService(self.credits)
        self.billing = (
            BillingServiceImpl(
                options.billing_store,
                (options.billing_options or BillingServiceOptions()).model_copy(update={"provisioning": self.credits}),
            )
            if options.billing_store is not None
            else None
        )
        self.commerce = (
            CommerceService(
                self.billing,
                self.credits,
                self,
                options.commerce_options,
            )
            if self.billing is not None and options.commerce_options is not None
            else None
        )
        if self.commerce is not None:

            async def process_auto_recharge(context) -> None:
                await self.commerce.auto_recharge.process_if_needed(AutoRechargeInput(account_id=context.user_id))

            self.credits.add_post_deduction_hook(process_auto_recharge)

    @classmethod
    def create(
        cls,
        *,
        credit_store: CreditStore,
        billing_store: BillingStore | None = None,
        credits: CreditsService | None = None,
        credits_options: CreditsServiceOptions | dict[str, Any] | None = None,
        billing_options: BillingServiceOptions | dict[str, Any] | None = None,
        commerce_options: CommerceOptions | None = None,
        emitter: CreditEventEmitter | None = None,
    ) -> Bursar:
        return cls(
            BursarOptions(
                credit_store=credit_store,
                billing_store=billing_store,
                credits=credits,
                credits_options=(
                    CreditsServiceOptions.model_validate(credits_options)
                    if isinstance(credits_options, dict)
                    else credits_options
                ),
                billing_options=(
                    BillingServiceOptions.model_validate(billing_options)
                    if isinstance(billing_options, dict)
                    else billing_options
                ),
                commerce_options=commerce_options,
                emitter=emitter,
            )
        )

    def load_catalog(self) -> None:
        """Load the active catalog into the pricing engine."""
        self.credits.load_pricing_from_store()

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        """Submit a normalized provider event through the facade."""
        if self.billing is None:
            raise RuntimeError("Bursar billing capability is not configured")
        return self.billing.ingest_billing_event(event)
