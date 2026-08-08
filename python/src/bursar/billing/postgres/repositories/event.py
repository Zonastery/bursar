from __future__ import annotations

from bursar.billing.postgres.repositories.schemas import BillingEventRow
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    require_boolean_result,
    require_mapping_row,
    validate_non_empty,
    validate_row,
)


class BillingEventRepository:
    """Repository for billing event lifecycle operations.

    All methods call Postgres via raw SQL queries through the query function.
    Mutation RPCs are fail-closed: a missing or malformed result raises
    :class:`~bursar.errors.StoreError`.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def claim(
        self,
        provider: str,
        event_id: str,
        event_type: str,
        metadata: str,
    ) -> BillingEventRow:
        """Claim a billing event for processing (idempotent).

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
            event_type: The event type string.
            metadata: JSON metadata string.

        Returns:
            The explicit lifecycle result returned by Postgres.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(event_id, "event_id")
        rows = self._execute(
            "SELECT * FROM bursar.claim_billing_event(%s, %s, %s, %s::jsonb)",
            [provider, event_id, event_type, metadata],
        )
        row = require_mapping_row(rows, "BillingEventRepository.claim")
        return validate_row(
            BillingEventRow,
            {
                "event_id": row.get("event_id"),
                "status": row.get("result"),
                "claim_token": row.get("claim_token"),
            },
            "BillingEventRepository.claim",
            indeterminate=True,
        )

    def complete(self, provider: str, event_id: str, claim_token: str) -> bool:
        """Mark a billing event as completed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        rows = self._execute(
            "SELECT bursar.complete_billing_event(%s, %s, %s::uuid) AS completed",
            [provider, event_id, claim_token],
        )
        return require_boolean_result(rows, "completed", "BillingEventRepository.complete")

    def fail(self, provider: str, event_id: str, claim_token: str, error: str | None = None) -> bool:
        """Mark a billing event as failed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        rows = self._execute(
            "SELECT bursar.fail_billing_event(%s, %s, %s::uuid, %s) AS failed",
            [provider, event_id, claim_token, error],
        )
        return require_boolean_result(rows, "failed", "BillingEventRepository.fail")
