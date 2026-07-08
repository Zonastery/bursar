from __future__ import annotations

from bursar.billing.models import (
    BillingConfig,
    BillingEventClaim,
    BillingSubscriptionState,
)
from bursar.billing.store import BillingStore


class MemoryBillingStore(BillingStore):
    def __init__(self) -> None:
        self._offers: dict[str, dict] = {}
        self._provider_refs: dict[tuple[str, str, str], str] = {}
        self._events: dict[tuple[str, str], str] = {}
        self._customers: dict[tuple[str, str], str] = {}
        self._subscriptions: dict[tuple[str, str], BillingSubscriptionState] = {}
        self._topups: dict[str, dict] = {}
        self._provider_refs_by: dict[tuple[str, str, str], str] = {}
        self._payments: dict[tuple[str, str], dict] = {}
        self._refunds: dict[tuple[str, str], dict] = {}
        self._invoices: dict[tuple[str, str], dict] = {}
        self._disputes: dict[tuple[str, str], dict] = {}

    def sync_billing_from_config(self, config: BillingConfig) -> None:
        self._offers.clear()
        self._provider_refs.clear()
        self._topups.clear()
        self._provider_refs_by.clear()

        for offer_key, offer in (config.subscriptions or {}).items():
            self._offers[offer_key] = offer.model_dump()
            for provider, ref in (offer.provider_refs or {}).items():
                if ref.price_id:
                    key = (provider, "price_id", ref.price_id)
                    self._provider_refs_by[key] = offer_key
                    self._provider_refs[key] = "offer"
                if ref.product_id:
                    key = (provider, "product_id", ref.product_id)
                    self._provider_refs_by.setdefault(key, offer_key)
                    self._provider_refs.setdefault(key, "offer")

        for topup_key, topup in (config.credit_topups or {}).items():
            self._topups[topup_key] = topup.model_dump()
            for provider, ref in (topup.provider_refs or {}).items():
                if ref.product_id:
                    key = (provider, "product_id", ref.product_id)
                    self._provider_refs_by[key] = topup_key
                    self._provider_refs[key] = "topup"
                if ref.price_id:
                    key = (provider, "price_id", ref.price_id)
                    self._provider_refs_by[key] = topup_key
                    self._provider_refs[key] = "topup"

    def resolve_billing_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None:
        if price_id:
            key = (provider, "price_id", price_id)
            offer_key = self._provider_refs_by.get(key)
            if offer_key:
                raw = self._offers.get(offer_key)
                if raw:
                    return {**raw, "offer_key": offer_key}
        if product_id:
            key = (provider, "product_id", product_id)
            offer_key = self._provider_refs_by.get(key)
            if offer_key:
                raw = self._offers.get(offer_key)
                if raw:
                    return {**raw, "offer_key": offer_key}
        return None

    def claim_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
    ) -> BillingEventClaim:
        key = (provider, event_id)
        existing = self._events.get(key)
        if existing is None:
            self._events[key] = "processing"
            return BillingEventClaim(status="claimed")
        if existing == "processing":
            return BillingEventClaim(status="retry")
        if existing == "failed":
            self._events[key] = "processing"
            return BillingEventClaim(status="claimed")
        return BillingEventClaim(status="duplicate")

    def complete_billing_event(self, provider: str, event_id: str) -> None:
        key = (provider, event_id)
        if key in self._events:
            self._events[key] = "completed"

    def fail_billing_event(self, provider: str, event_id: str) -> None:
        key = (provider, event_id)
        if key in self._events:
            self._events[key] = "failed"

    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        key = (provider, provider_customer_id)
        self._customers[key] = user_id

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        key = (state.provider, state.provider_subscription_id)
        self._subscriptions[key] = state

    def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None:
        key = (provider, provider_customer_id)
        return self._customers.get(key)

    def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None:
        key = (provider, provider_subscription_id)
        return self._subscriptions.get(key)

    def get_user_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None:
        latest: BillingSubscriptionState | None = None
        for sub in self._subscriptions.values():
            if sub.user_id == user_id and (
                latest is None
                or (
                    sub.current_period_start
                    and (latest.current_period_start is None or sub.current_period_start > latest.current_period_start)
                )
            ):
                latest = sub
        return latest

    def resolve_credit_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None:
        if price_id:
            key = (provider, "price_id", price_id)
            topup_key = self._provider_refs_by.get(key)
            if topup_key:
                raw = self._topups.get(topup_key)
                if raw:
                    return {**raw, "topup_key": topup_key}
        if product_id:
            key = (provider, "product_id", product_id)
            topup_key = self._provider_refs_by.get(key)
            if topup_key:
                raw = self._topups.get(topup_key)
                if raw:
                    return {**raw, "topup_key": topup_key}
        return None

    def upsert_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
        provider_invoice_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        tax_minor: int | None = None,
        currency: str = "USD",
        purpose: str = "unknown",
        metadata: dict | None = None,
    ) -> None:
        key = (provider, provider_payment_id)
        self._payments[key] = {
            "provider": provider,
            "provider_payment_id": provider_payment_id,
            "provider_invoice_id": provider_invoice_id,
            "user_id": user_id,
            "amount_minor": amount_minor,
            "tax_minor": tax_minor,
            "currency": currency,
            "purpose": purpose,
            "metadata": metadata,
        }

    def upsert_billing_refund(
        self,
        provider: str,
        provider_refund_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        currency: str = "USD",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        key = (provider, provider_refund_id)
        self._refunds[key] = {
            "provider": provider,
            "provider_refund_id": provider_refund_id,
            "provider_payment_id": provider_payment_id,
            "user_id": user_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "reason": reason,
            "metadata": metadata,
        }

    def upsert_billing_invoice(
        self,
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
    ) -> None:
        key = (provider, provider_invoice_id)
        self._invoices[key] = {
            "provider": provider,
            "provider_invoice_id": provider_invoice_id,
            "provider_subscription_id": provider_subscription_id,
            "user_id": user_id,
            "status": status,
            "amount_paid_minor": amount_paid_minor,
            "amount_due_minor": amount_due_minor,
            "currency": currency,
            "period_start": period_start,
            "period_end": period_end,
            "metadata": metadata,
        }

    def upsert_billing_dispute(
        self,
        provider: str,
        provider_dispute_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        status: str = "needs_response",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        key = (provider, provider_dispute_id)
        self._disputes[key] = {
            "provider": provider,
            "provider_dispute_id": provider_dispute_id,
            "provider_payment_id": provider_payment_id,
            "user_id": user_id,
            "status": status,
            "reason": reason,
            "metadata": metadata,
        }

    def get_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> dict | None:
        key = (provider, provider_payment_id)
        return self._payments.get(key)

    def get_billing_refund(
        self,
        provider: str,
        provider_refund_id: str,
    ) -> dict | None:
        key = (provider, provider_refund_id)
        return self._refunds.get(key)

    def get_billing_invoice(
        self,
        provider: str,
        provider_invoice_id: str,
    ) -> dict | None:
        key = (provider, provider_invoice_id)
        return self._invoices.get(key)

    def get_billing_dispute(
        self,
        provider: str,
        provider_dispute_id: str,
    ) -> dict | None:
        key = (provider, provider_dispute_id)
        return self._disputes.get(key)
