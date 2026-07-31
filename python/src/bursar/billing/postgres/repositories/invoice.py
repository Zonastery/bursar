from __future__ import annotations

from bursar.billing.types import BillingInvoiceInfo
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty


class BillingInvoiceRepository:
    """Repository for billing invoice operations.

    All methods call Postgres via raw SQL queries through the query function.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def list_for_user(self, user_id: str) -> list[BillingInvoiceInfo]:
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            "SELECT * FROM bursar.list_billing_invoices(%s::uuid)",
            [user_id],
        )
        return [
            BillingInvoiceInfo(
                provider=str(row.get("provider") or ""),
                provider_invoice_id=str(row.get("provider_invoice_id") or ""),
                status=str(row["status"]) if row.get("status") is not None else None,
                amount_paid_minor=(int(row["amount_paid_minor"]) if row.get("amount_paid_minor") is not None else None),
                amount_due_minor=(int(row["amount_due_minor"]) if row.get("amount_due_minor") is not None else None),
                currency=(str(row["currency"]) if row.get("currency") is not None else None),
                period_start=(str(row["period_start"]) if row.get("period_start") is not None else None),
                period_end=(str(row["period_end"]) if row.get("period_end") is not None else None),
            )
            for row in rows
            if isinstance(row, dict)
        ]

    def upsert(
        self,
        subject_id: str,
        provider: str,
        provider_invoice_id: str,
        subscription_id: str | None,
        status: str | None,
        amount_due_minor: int | None,
        amount_paid_minor: int | None,
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
        self._execute(
            "SELECT bursar.upsert_billing_invoice(%s::uuid,%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
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
