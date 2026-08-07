from __future__ import annotations

from typing import Literal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import require_identifier_result, validate_non_empty
from bursar.errors import StoreError


class BillingRefundRepository:
    """Repository for billing refund operations.

    All methods call Postgres via raw SQL queries through the query function.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_refund_id: str,
        provider_payment_id: str,
        user_id: str,
        amount_minor: int,
        currency: str,
        reason: str | None,
        metadata: str | None,
        status: Literal["pending", "succeeded", "failed", "canceled"],
        provider_updated_at: str,
    ) -> str:
        """Insert or update a billing refund record.

        Args:
            provider: The billing provider identifier.
            provider_refund_id: The provider refund ID.
            provider_payment_id: The associated provider payment ID, or None.
            user_id: The user ID, or None.
            amount_minor: The refund amount in minor currency units.
            currency: The ISO 4217 currency code.
            reason: The refund reason, or None.
            metadata: JSON metadata string, or None.
            status: The refund status (default "pending").
            provider_updated_at: Optional provider timestamp.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_refund_id, "provider_refund_id")
        if not provider_payment_id:
            raise ValueError("refund requires provider_payment_id")
        payments = self._execute(
            "SELECT * FROM bursar.get_billing_payment_by_provider(%s, %s)",
            [provider, provider_payment_id],
        )
        if not payments or not isinstance(payments[0], dict) or not payments[0].get("id"):
            raise StoreError(
                "refund payment not found",
                retryable=True,
                details={"provider": provider, "provider_payment_id": provider_payment_id},
            )
        rows = self._execute(
            "SELECT bursar.upsert_billing_refund(%s::uuid, %s, %s, %s, %s, %s, %s::uuid, %s::char(3), %s::jsonb) AS id",
            [
                payments[0]["id"],
                provider_refund_id,
                amount_minor,
                status,
                reason,
                provider_updated_at,
                user_id,
                currency,
                metadata or "{}",
            ],
        )
        return require_identifier_result(rows, "id", "BillingRefundRepository.upsert")
