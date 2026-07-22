from abc import ABC, abstractmethod
from typing import Any

from bursar.billing.models import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingConfig,
    BillingCustomerRecord,
    BillingEventClaim,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionState,
    BillingTopupResult,
    CheckoutIntent,
)


class BillingStore(ABC):
    @abstractmethod
    def sync_billing_from_config(self, config: BillingConfig) -> None: ...

    @abstractmethod
    def create_or_get_checkout_intent(
        self,
        actor_key: str,
        provider: str,
        type: str,
        product_id: str,
        request_fingerprint: str,
        expires_at: str,
    ) -> CheckoutIntent: ...

    @abstractmethod
    def update_checkout_intent(
        self,
        id: str,
        status: str | None = None,
        provider_session_id: str | None = None,
        checkout_url: str | None = None,
    ) -> None: ...

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
    def complete_billing_event(self, provider: str, event_id: str, claim_token: str) -> None: ...

    @abstractmethod
    def fail_billing_event(self, provider: str, event_id: str, claim_token: str, error: str | None = None) -> None: ...

    @abstractmethod
    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> dict[str, Any]: ...

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
        statuses: list[str] | None = None,
    ) -> BillingSubscriptionState | None: ...

    @abstractmethod
    def create_billing_subscription_change(
        self,
        change: BillingSubscriptionChange,
    ) -> BillingSubscriptionChange: ...

    @abstractmethod
    def get_open_billing_subscription_change(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionChange | None: ...

    @abstractmethod
    def update_billing_subscription_change(self, id: str, **updates: Any) -> None: ...

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
    def record_subscription_conflict(
        self,
        user_id: str | None = None,
        provider: str = "",
        duplicate_subscription_id: str = "",
        existing_subscription_id: str | None = None,
        event_id: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

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
    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None: ...

    @abstractmethod
    def upsert_auto_recharge_profile(self, profile: BillingAutoRechargeProfile) -> None: ...

    @abstractmethod
    def claim_auto_recharge_attempt(
        self,
        user_id: str,
        provider: str,
        topup_key: str,
        quantity: int,
        max_recharges: int,
        window_days: int,
    ) -> BillingAutoRechargeAttempt | None: ...

    @abstractmethod
    def update_auto_recharge_attempt(
        self,
        attempt_id: str,
        state: str,
        provider_payment_id: str | None = None,
        failure_code: str | None = None,
        action_url: str | None = None,
    ) -> None: ...

    @abstractmethod
    def get_billing_customer_by_user_id(
        self,
        user_id: str,
        provider: str | None = None,
    ) -> BillingCustomerRecord | None: ...
