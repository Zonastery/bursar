from __future__ import annotations

from bursar.repositories._types import QueryFn


class BillingInvoiceRepository:
    """Repository for billing invoice operations.

    All methods call Postgres via raw SQL queries through the query function.
    """

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
