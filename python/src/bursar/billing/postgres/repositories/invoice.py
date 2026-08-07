from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from bursar.billing.types import BillingInvoiceRecord
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import require_identifier_result, validate_non_empty
from bursar.errors import StoreError


def _optional_utc_iso(value: object) -> str | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("invoice timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


class BillingInvoiceRepository:
    """Repository for billing invoice operations.

    All methods call Postgres via raw SQL queries through the query function.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def list_for_user(self, user_id: str) -> list[BillingInvoiceRecord]:
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            "SELECT * FROM bursar.list_billing_invoices(%s::uuid)",
            [user_id],
        )
        invoices: list[BillingInvoiceRecord] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise StoreError(
                    "BillingInvoiceRepository.list_for_user: expected an object row",
                    details={"row_index": index},
                )
            try:
                payload = dict(row)
                payload.update(
                    {
                        "period_start": _optional_utc_iso(row.get("period_start")),
                        "period_end": _optional_utc_iso(row.get("period_end")),
                    }
                )
                invoices.append(BillingInvoiceRecord.model_validate(payload))
            except (ValidationError, ValueError) as exc:
                raise StoreError(
                    "BillingInvoiceRepository.list_for_user: result validation failed",
                    cause=exc,
                    details={"row_index": index},
                ) from exc
        return invoices

    def upsert(
        self,
        subject_id: str,
        provider: str,
        provider_invoice_id: str,
        subscription_id: str | None,
        status: Literal["draft", "open", "paid", "void", "uncollectible"],
        amount_due_minor: int,
        amount_paid_minor: int,
        currency: str,
        period_start: str | None,
        period_end: str | None,
        metadata: str,
        provider_updated_at: str,
    ) -> None:
        """Insert or update a billing invoice record.

        Args:
            provider: The billing provider identifier.
            provider_invoice_id: The provider invoice ID.
            provider_subscription_id: The associated subscription ID, or None.
            user_id: The user ID, or None.
            status: The invoice status, or None.
            amount_paid_minor: Amount paid in minor currency units, or None.
            amount_due_minor: Amount due in minor currency units, or None.
            currency: The ISO 4217 currency code.
            period_start: The billing period start, or None.
            period_end: The billing period end, or None.
            metadata: JSON metadata string, or None.
        """
        validate_non_empty(subject_id, "subject_id")
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_invoice_id, "provider_invoice_id")
        rows = self._execute(
            "SELECT bursar.upsert_billing_invoice(%s::uuid,%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) AS id",
            [
                subject_id,
                provider,
                provider_invoice_id,
                subscription_id,
                status,
                amount_due_minor,
                amount_paid_minor,
                currency,
                period_start,
                period_end,
                metadata,
                provider_updated_at,
            ],
        )
        require_identifier_result(rows, "id", "BillingInvoiceRepository.upsert")
