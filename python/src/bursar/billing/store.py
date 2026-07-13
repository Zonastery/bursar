from abc import ABC, abstractmethod
from typing import Any

from bursar.billing.models import (
    BillingConfig,
    BillingCustomerRecord,
    BillingEventClaim,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionState,
    BillingTopupResult,
)


class BillingStore(ABC):
    @abstractmethod
    def sync_billing_from_config(self, config: BillingConfig) -> None: ...

    @abstractmethod
    def resolve_billing_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> BillingOfferResult | None: ...

    @abstractmethod
    def claim_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
    ) -> BillingEventClaim: ...

    @abstractmethod
    def complete_billing_event(self, provider: str, event_id: str) -> None: ...

    @abstractmethod
    def fail_billing_event(self, provider: str, event_id: str) -> None: ...

    @abstractmethod
    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None: ...

    @abstractmethod
    def upsert_billing_subscription(
        self,
        state: BillingSubscriptionState,
    ) -> None: ...

    @abstractmethod
    def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None: ...

    @abstractmethod
    def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None: ...

    @abstractmethod
    def get_user_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None: ...

    @abstractmethod
    def resolve_credit_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> BillingTopupResult | None: ...

    @abstractmethod
    def resolve_billing_offer_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingOfferResult | None: ...

    @abstractmethod
    def resolve_credit_topup_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingTopupResult | None: ...

    @abstractmethod
    def upsert_billing_payment(
        self,
        *,
        provider: str,
        provider_payment_id: str,
        provider_invoice_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        tax_minor: int | None = None,
        currency: str = "USD",
        purpose: str = "unknown",
        metadata: dict | None = None,
    ) -> None: ...

    @abstractmethod
    def upsert_billing_refund(
        self,
        *,
        provider: str,
        provider_refund_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        currency: str = "USD",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

    @abstractmethod
    def upsert_billing_invoice(
        self,
        *,
        provider: str,
        provider_invoice_id: str,
        provider_subscription_id: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        amount_paid_minor: int | None = None,
        amount_due_minor: int | None = None,
        currency: str = "USD",
        period_start: str | None = None,
        period_end: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

    @abstractmethod
    def upsert_billing_dispute(
        self,
        *,
        provider: str,
        provider_dispute_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        status: str = "needs_response",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

    @abstractmethod
    def get_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> dict | None: ...

    @abstractmethod
    def get_user_subscriptions(self, user_id: str) -> list[BillingSubscriptionState]: ...

    @abstractmethod
    def deactivate_other_provider_subscriptions(
        self,
        user_id: str,
        keep_provider: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_billing_preferences(self, user_id: str) -> BillingPreferences | None: ...

    @abstractmethod
    def upsert_billing_preferences(self, prefs: BillingPreferences) -> None: ...

    @abstractmethod
    def get_billing_customer_by_user_id(
        self,
        user_id: str,
        provider: str | None = None,
    ) -> BillingCustomerRecord | None: ...
