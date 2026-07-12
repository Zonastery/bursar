from __future__ import annotations

from typing import Any

from bursar.repositories._types import QueryFn
from bursar.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.repositories.schemas import BillingPaymentRow


class BillingPaymentRepository:
    """Repository for billing payment operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

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
        """Insert or update a billing payment record.

        Args:
            provider: The billing provider identifier.
            provider_payment_id: The provider payment ID.
            provider_invoice_id: The associated provider invoice ID, or None.
            user_id: The user ID, or None.
            amount_minor: The payment amount in minor currency units.
            tax_minor: The tax amount in minor currency units, or None.
            currency: The ISO 4217 currency code.
            purpose: The payment purpose, or None.
            metadata: JSON metadata string, or None.
        """
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
        """Get payment details for refund processing.

        Args:
            provider: The billing provider identifier.
            provider_payment_id: The provider payment ID.

        Returns:
            Payment details dict if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_payment_id, "provider_payment_id")
        return unwrap_jsonb(
            self._execute(
                "SELECT * FROM public.get_billing_payment_for_refund(%s, %s)",
                [provider, provider_payment_id],
            )
        )

    def get_direct(self, provider: str, provider_payment_id: str) -> BillingPaymentRow | None:
        """Get a billing payment directly from the payments table.

        Args:
            provider: The billing provider identifier.
            provider_payment_id: The provider payment ID.

        Returns:
            BillingPaymentRow if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_payment_id, "provider_payment_id")
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
