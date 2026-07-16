from __future__ import annotations

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.repositories.schemas import BillingEventRow


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
        row = unwrap_jsonb(
            self._execute(
                "SELECT * FROM bursar.claim_billing_event(%s, %s, %s, %s)",
                [provider, event_id, event_type, metadata],
            )
        )
        return BillingEventRow.model_validate(row) if row else None

    def complete(self, provider: str, event_id: str, claim_token: str) -> None:
        """Mark a billing event as completed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        self._execute("SELECT bursar.complete_billing_event(%s, %s, %s::uuid)", [provider, event_id, claim_token])

    def fail(self, provider: str, event_id: str, claim_token: str, error: str | None = None) -> None:
        """Mark a billing event as failed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        self._execute(
            "SELECT bursar.fail_billing_event(%s, %s, %s::uuid, %s)", [provider, event_id, claim_token, error]
        )
