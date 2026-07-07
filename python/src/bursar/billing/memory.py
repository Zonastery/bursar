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
        self._customers: dict[str, str] = {}
        self._subscriptions: dict[str, BillingSubscriptionState] = {}
        self._topups: dict[str, dict] = {}
        self._provider_refs_by: dict[tuple[str, str], str] = {}

    async def sync_billing_from_config(self, config: BillingConfig) -> None:
        self._offers.clear()
        self._provider_refs.clear()
        self._topups.clear()
        self._provider_refs_by.clear()

        for offer_key, offer in (config.subscriptions or {}).items():
            self._offers[offer_key] = offer.model_dump()
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

        subscriptions = config.subscriptions or {}
        for offer_key, offer in subscriptions.items():
            refs = getattr(offer, "provider_refs", None)
            if refs:
                for provider, ref in refs.items():
                    if isinstance(ref, dict):
                        if ref.get("price_id"):
                            key = (provider, "price_id", ref["price_id"])
                            self._provider_refs_by[key] = offer_key
                            self._provider_refs[key] = "offer"
                        if ref.get("product_id"):
                            key = (provider, "product_id", ref["product_id"])
                            self._provider_refs_by.setdefault(key, offer_key)
                            self._provider_refs.setdefault(key, "offer")
                    else:
                        if ref.price_id:
                            key = (provider, "price_id", ref.price_id)
                            self._provider_refs_by[key] = offer_key
                            self._provider_refs[key] = "offer"
                        if ref.product_id:
                            key = (provider, "product_id", ref.product_id)
                            self._provider_refs_by.setdefault(key, offer_key)
                            self._provider_refs.setdefault(key, "offer")

    async def resolve_billing_offer(
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

    async def claim_billing_event(
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
        if existing == "completed":
            return BillingEventClaim(status="duplicate")
        return BillingEventClaim(status="duplicate")

    async def complete_billing_event(self, provider: str, event_id: str) -> None:
        key = (provider, event_id)
        if key in self._events:
            self._events[key] = "completed"

    async def fail_billing_event(self, provider: str, event_id: str) -> None:
        key = (provider, event_id)
        if key in self._events:
            self._events[key] = "failed"

    async def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        key = (provider, provider_customer_id)
        self._customers[key] = user_id

    async def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        key = (state.provider, state.provider_subscription_id)
        self._subscriptions[key] = state

    async def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None:
        key = (provider, provider_customer_id)
        return self._customers.get(key)

    async def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None:
        key = (provider, provider_subscription_id)
        return self._subscriptions.get(key)

    async def resolve_credit_topup(
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

    async def compute_topup_credits(self, amount_minor: int, topup_config: dict) -> int:
        major_amount = amount_minor / 100
        credits_per = topup_config.get("credits_per_major_unit", 1000)
        return int(major_amount * credits_per)
