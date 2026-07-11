from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import BillingPaymentRow

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingPaymentRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_payment_id: str,
        provider_invoice_id: str | None,
        user_id: str | None,
        amount_minor: int,
        tax_minor: int | None,
        currency: str,
        purpose: str | None,
        metadata: str | None,
    ) -> None:
        self._execute(
            "SELECT public.upsert_billing_payment(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                provider,
                provider_payment_id,
                provider_invoice_id,
                user_id,
                amount_minor,
                tax_minor,
                currency,
                purpose,
                metadata,
            ],
        )

    def get_for_refund(self, provider: str, provider_payment_id: str) -> dict[str, Any] | None:
        return self._unwrap_jsonb(
            self._execute(
                "SELECT * FROM public.get_billing_payment_for_refund(%s, %s)",
                [provider, provider_payment_id],
            )
        )

    def get_direct(self, provider: str, provider_payment_id: str) -> BillingPaymentRow | None:
        rows = self._execute(
            """SELECT provider, provider_payment_id, user_id, amount_minor,
                      tax_minor, currency, purpose, metadata, created_at, updated_at
               FROM public.billing_payments
               WHERE provider = %s AND provider_payment_id = %s""",
            [provider, provider_payment_id],
        )
        if not rows:
            return None
        return BillingPaymentRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    @staticmethod
    def _unwrap_jsonb(rows: list[Any]) -> dict[str, Any] | None:
        if not rows or len(rows) != 1:
            return None
        row = rows[0]
        if isinstance(row, dict):
            keys = list(row.keys())
            if len(keys) == 1:
                v = row[keys[0]]
                if isinstance(v, dict):
                    return v
            return row
        return None
