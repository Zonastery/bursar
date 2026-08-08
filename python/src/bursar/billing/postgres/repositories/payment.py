from __future__ import annotations

from typing import Literal

from bursar.billing.postgres.repositories.schemas import BillingPaymentRow
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_identifier_result,
    validate_non_empty,
    validate_row,
)


class BillingPaymentRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_payment_id: str,
        provider_invoice_id: str | None,
        user_id: str,
        amount_minor: int,
        tax_minor: int,
        currency: str,
        purpose: Literal["subscription", "credit_topup"],
        metadata: str | None,
        status: Literal["pending", "succeeded", "failed", "canceled"],
        provider_updated_at: str,
    ) -> str:
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_payment_id, "provider_payment_id")
        if not user_id:
            raise ValueError("billing payment requires a subject")
        if purpose not in {"subscription", "credit_topup"}:
            raise ValueError("billing payment requires a known purpose")
        rows = self._execute(
            "SELECT bursar.upsert_billing_payment("
            "%s::uuid,%s,%s,%s,%s,%s,%s,%s::bursar.billing_payment_status,%s,%s,%s::jsonb"
            ") AS id",
            [
                user_id,
                provider,
                provider_payment_id,
                amount_minor,
                tax_minor,
                currency,
                purpose,
                status,
                provider_updated_at,
                provider_invoice_id,
                metadata or "{}",
            ],
        )
        return require_identifier_result(rows, "id", "BillingPaymentRepository.upsert")

    def get_for_refund(self, provider: str, provider_payment_id: str) -> BillingPaymentRow | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_payment_by_provider(%s,%s)",
            [provider, provider_payment_id],
        )
        row = optional_mapping_row(rows, "BillingPaymentRepository.get_for_refund")
        if row is None:
            return None
        return validate_row(
            BillingPaymentRow,
            {
                "id": row.get("id"),
                "provider": row.get("provider"),
                "provider_payment_id": row.get("provider_payment_id"),
                "provider_invoice_id": row.get("provider_invoice_id"),
                "subject_id": row.get("subject_id"),
                "amount_minor": row.get("amount_minor"),
                "tax_minor": row.get("tax_minor"),
                "currency": row.get("currency"),
                "purpose": row.get("purpose"),
                "status": row.get("status"),
                "provider_updated_at": row.get("provider_updated_at"),
                "metadata": row.get("metadata"),
            },
            "BillingPaymentRepository.get_for_refund",
        )

    def get_direct(self, provider: str, provider_payment_id: str) -> BillingPaymentRow | None:
        return self.get_for_refund(provider, provider_payment_id)
