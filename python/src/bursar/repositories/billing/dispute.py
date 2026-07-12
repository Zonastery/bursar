from __future__ import annotations

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty


class BillingDisputeRepository:
    """Repository for billing dispute operations.

    All methods call Postgres via raw SQL queries through the query function.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_dispute_id: str,
        provider_payment_id: str | None,
        user_id: str | None,
        status: str,
        reason: str | None,
        metadata: str | None,
    ) -> None:
        """Insert or update a billing dispute record.

        Args:
            provider: The billing provider identifier.
            provider_dispute_id: The provider dispute ID.
            provider_payment_id: The associated provider payment ID, or None.
            user_id: The user ID, or None.
            status: The dispute status string.
            reason: The dispute reason, or None.
            metadata: JSON metadata string, or None.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_dispute_id, "provider_dispute_id")
        self._execute(
            "SELECT public.upsert_billing_dispute(%s, %s, %s, %s, %s, %s, %s)",
            [provider, provider_dispute_id, provider_payment_id, user_id, status, reason, metadata],
        )
