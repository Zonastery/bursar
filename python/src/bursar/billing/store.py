from __future__ import annotations

from abc import ABC, abstractmethod

from bursar.billing.models import (
    BillingConfig,
    BillingEventClaim,
    BillingSubscriptionState,
)


class BillingStore(ABC):
    @abstractmethod
    async def sync_billing_from_config(self, config: BillingConfig) -> None: ...

    @abstractmethod
    async def resolve_billing_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None: ...

    @abstractmethod
    async def claim_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
    ) -> BillingEventClaim: ...

    @abstractmethod
    async def complete_billing_event(self, provider: str, event_id: str) -> None: ...

    @abstractmethod
    async def fail_billing_event(self, provider: str, event_id: str) -> None: ...

    @abstractmethod
    async def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def upsert_billing_subscription(
        self,
        state: BillingSubscriptionState,
    ) -> None: ...

    @abstractmethod
    async def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None: ...

    @abstractmethod
    async def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None: ...

    @abstractmethod
    async def resolve_credit_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None: ...

    @abstractmethod
    async def compute_topup_credits(
        self,
        amount_minor: int,
        topup_config: dict,
    ) -> int:
        """Convert paid amount to credits based on topup config."""
        ...
