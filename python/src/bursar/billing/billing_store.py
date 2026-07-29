from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from bursar.billing.types import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCustomerRecord,
    BillingEventClaim,
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


class BillingStore(ABC):
    @abstractmethod
    def get_active_bursar_config(self) -> dict[str, Any] | None: ...
    @abstractmethod
    def create_or_get_checkout_intent(
        self,
        subject_id: str,
        provider: str,
        checkout_kind: str,
        product_key: str,
        request_digest: str,
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
    def create_billing_credit_grant(
        self,
        *,
        payment_id: str | None = None,
        subscription_id: str | None = None,
        topup_id: str | None = None,
        configured_credits: str,
        quantity: int = 1,
        billing_event_id: str | None = None,
    ) -> str: ...

    @abstractmethod
    def grant_billing_credit(self, grant_id: str, idempotency_key: str) -> dict: ...

    @abstractmethod
    def get_billing_credit_grant_by_payment(self, payment_id: str) -> str | None: ...

    @abstractmethod
    def post_billing_refund(self, refund_id: str, grant_id: str, amount_minor: int, idempotency_key: str) -> dict: ...

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
        envelope: dict[str, Any] | None = None,
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
        input: BillingSubscriptionChangeInput,
    ) -> BillingSubscriptionChange: ...

    @abstractmethod
    def get_open_billing_subscription_change(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionChange | None: ...

    @abstractmethod
    def update_billing_subscription_change(
        self,
        id: str,
        *,
        state: BillingSubscriptionChangeState | None = None,
        provider_operation_id: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

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
        status: str = "succeeded",
        provider_updated_at: str | None = None,
        metadata: dict | None = None,
    ) -> str: ...

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
        status: str = "pending",
        provider_updated_at: str | None = None,
        metadata: dict | None = None,
    ) -> str: ...

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
        provider_updated_at: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

    @abstractmethod
    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceInfo]: ...

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
        provider_updated_at: str | None = None,
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
        idempotency_key: str,
    ) -> BillingAutoRechargeAttempt | None: ...

    @abstractmethod
    def update_auto_recharge_attempt(
        self,
        attempt_id: str,
        state: str,
        provider_attempt_id: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def get_billing_customer_by_user_id(
        self,
        user_id: str,
        provider: str | None = None,
    ) -> BillingCustomerRecord | None: ...

    @abstractmethod
    def get_checkout_intent(self, id: str, subject_id: str) -> CheckoutIntent | None: ...

    @abstractmethod
    def list_expired_grace_subscriptions(self, now: datetime, limit: int = 100) -> list[dict]: ...

    @abstractmethod
    def mark_subscription_grace_expired(self, id: str, expected_grace_ends_at: str, expired_at: str) -> bool: ...

    @abstractmethod
    def deactivate_other_provider_subscriptions(
        self, user_id: str, keep_provider: str, subscription_id: str | None = None
    ) -> dict: ...

    @abstractmethod
    def record_subscription_conflict(
        self,
        user_id: str | None,
        provider: str,
        duplicate_subscription_id: str,
        existing_subscription_id: str | None = None,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def compute_topup_credits(self, amount_minor: int, topup_config: BillingTopupResult) -> Decimal: ...

    @abstractmethod
    def pseudonymize_financial_subject(self, user_id: str) -> bool: ...

    @abstractmethod
    def update_auto_recharge_attempt_by_provider_payment(
        self,
        provider: str,
        provider_payment_id: str,
        state: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None: ...

    @abstractmethod
    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime | int | float,
    ) -> int: ...
