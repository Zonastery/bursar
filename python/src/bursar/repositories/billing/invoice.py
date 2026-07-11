from __future__ import annotations

from collections.abc import Callable
from typing import Any

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingInvoiceRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_invoice_id: str,
        provider_subscription_id: str | None,
        user_id: str | None,
        status: str | None,
        amount_paid_minor: int | None,
        amount_due_minor: int | None,
        currency: str,
        period_start: str | None,
        period_end: str | None,
        metadata: str | None,
    ) -> None:
        self._execute(
            "SELECT public.upsert_billing_invoice(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                provider,
                provider_invoice_id,
                provider_subscription_id,
                user_id,
                status,
                amount_paid_minor,
                amount_due_minor,
                currency,
                period_start,
                period_end,
                metadata,
            ],
        )
