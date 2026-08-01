from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from bursar.billing.contracts import (
    AutoRechargeAttemptClaim,
    AutoRechargeAttemptUpdate,
    AutoRechargeProviderPaymentUpdate,
    BillingCreditGrantCreate,
    BillingDisputeUpsert,
    BillingInvoiceUpsert,
    BillingPaymentUpsert,
    BillingRefundUpsert,
    BillingSubscriptionChangeUpdate,
    BillingSubscriptionConflictCreate,
    CheckoutIntentCreate,
    CheckoutIntentUpdate,
)
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
        input: CheckoutIntentCreate,
    ) -> CheckoutIntent: ...

    @abstractmethod
    def update_checkout_intent(
        self,
        id: str,
        update: CheckoutIntentUpdate,
    ) -> None: ...

    @abstractmethod
    def create_billing_credit_grant(
        self,
        input: BillingCreditGrantCreate,
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
        update: BillingSubscriptionChangeUpdate,
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
        input: BillingPaymentUpsert,
    ) -> str: ...

    @abstractmethod
    def upsert_billing_refund(
        self,
        input: BillingRefundUpsert,
    ) -> str: ...

    @abstractmethod
    def upsert_billing_invoice(
        self,
        input: BillingInvoiceUpsert,
    ) -> None: ...

    @abstractmethod
    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceInfo]: ...

    @abstractmethod
    def upsert_billing_dispute(
        self,
        input: BillingDisputeUpsert,
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
    def upsert_auto_recharge_profile(
        self,
        profile: BillingAutoRechargeProfile,
        *,
        reset_cooldown: bool = False,
    ) -> None: ...

    @abstractmethod
    def claim_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptClaim,
    ) -> BillingAutoRechargeAttempt | None: ...

    @abstractmethod
    def update_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptUpdate,
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
    def select_subscription_entitlement_source(
        self, user_id: str, provider: str, subscription_id: str | None = None
    ) -> bool: ...

    @abstractmethod
    def record_subscription_conflict(
        self,
        input: BillingSubscriptionConflictCreate,
    ) -> None: ...

    @abstractmethod
    def compute_topup_credits(self, amount_minor: int, topup_config: BillingTopupResult) -> Decimal: ...

    @abstractmethod
    def pseudonymize_financial_subject(self, user_id: str) -> bool: ...

    @abstractmethod
    def update_auto_recharge_attempt_by_provider_payment(
        self,
        input: AutoRechargeProviderPaymentUpdate,
    ) -> None: ...

    @abstractmethod
    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime,
    ) -> int: ...
