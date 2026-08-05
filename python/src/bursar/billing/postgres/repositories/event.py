from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty
from bursar.credits.postgres.repositories.schemas import BillingEventRow


class BillingEventRepository:
    """Repository for billing event lifecycle operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def claim(
        self,
        provider: str,
        event_id: str,
        event_type: str,
        metadata: str,
    ) -> BillingEventRow | None:
        """Claim a billing event for processing (idempotent).

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
            event_type: The event type string.
            metadata: JSON metadata string.

        Returns:
            BillingEventRow if claimed successfully, None if already claimed.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(event_id, "event_id")
        rows = self._execute(
            "SELECT * FROM bursar.claim_billing_event(%s, %s, %s, %s::jsonb)",
            [provider, event_id, event_type, metadata],
        )
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            return None
        return BillingEventRow.model_validate(
            {
                "event_id": row.get("event_id"),
                "status": row.get("result", "retry"),
                "claim_token": row.get("claim_token"),
                "provider": provider,
            }
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
        return bool(rows and isinstance(rows[0], dict) and rows[0].get("completed") is True)

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
        return bool(rows and isinstance(rows[0], dict) and rows[0].get("failed") is True)
