from __future__ import annotations

from collections.abc import Callable
from typing import Any

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingDisputeRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_dispute_id: str,
        provider_payment_id: str | None,
        user_id: str | None,
        status: str,
        reason: str | None,
        metadata: str | None,
    ) -> None:
        self._execute(
            "SELECT public.upsert_billing_dispute(%s, %s, %s, %s, %s, %s, %s)",
            [provider, provider_dispute_id, provider_payment_id, user_id, status, reason, metadata],
        )
