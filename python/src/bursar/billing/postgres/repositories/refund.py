from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty


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
        provider_payment_id: str | None,
        user_id: str | None,
        amount_minor: int,
        currency: str,
        reason: str | None,
        metadata: str | None,
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
            raise ValueError("refund payment not found")
        rows = self._execute(
            "SELECT bursar.upsert_billing_refund(%s::uuid, %s, %s)",
            [payments[0]["id"], provider_refund_id, amount_minor],
        )
        return str(next(iter(rows[0].values())))
