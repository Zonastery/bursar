from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty
from bursar.credits.postgres.repositories.schemas import BillingPaymentRow


class BillingPaymentRepository:
    def __init__(self, execute: DbQuery) -> None:
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
    ) -> str:
        del provider_invoice_id, metadata
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_payment_id, "provider_payment_id")
        rows = self._execute(
            ("SELECT bursar.upsert_billing_payment(%s::uuid,%s,%s,%s,%s,%s,%s,%s::bursar.billing_payment_status)"),
            [
                user_id,
                provider,
                provider_payment_id,
                amount_minor,
                tax_minor or 0,
                currency,
                purpose or "unknown",
                "succeeded",
            ],
        )
        if not rows or not isinstance(rows[0], dict):
            raise ValueError("billing payment upsert returned no ID")
        return str(next(iter(rows[0].values())))

    def get_for_refund(self, provider: str, provider_payment_id: str) -> BillingPaymentRow | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_payment_by_provider(%s,%s)",
            [provider, provider_payment_id],
        )
        return BillingPaymentRow.model_validate(rows[0]) if rows and isinstance(rows[0], dict) else None

    def get_direct(self, provider: str, provider_payment_id: str) -> BillingPaymentRow | None:
        return self.get_for_refund(provider, provider_payment_id)
