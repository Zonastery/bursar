from __future__ import annotations

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty
from bursar.repositories.schemas import AddCreditsRow, AvailableRow, BalanceRow


class BalanceRepository:
    """Repository for user credit balance operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def get_balance(self, user_id: str) -> BalanceRow | None:
        """Get the credit balance for a user.

        Args:
            user_id: The user ID.

        Returns:
            BalanceRow if found, None if the user has no balance record.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_credits_balance", [user_id])
        if not rows:
            return None
        return BalanceRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def add_credits(
        self,
        user_id: str,
        amount: str,
        type_: str,
        metadata: str,
        bucket: str | None,
        idempotency_key: str | None,
    ) -> AddCreditsRow | None:
        """Add credits to a user's balance.

        Args:
            user_id: The user ID.
            amount: The credit amount as a string (Decimal-safe).
            type_: The transaction type (e.g. "purchase", "adjustment").
            metadata: JSON metadata string.
            bucket: The target bucket key, or None for default.
            idempotency_key: Idempotency key for replay protection, or None.

        Returns:
            AddCreditsRow if successful, None if the RPC returned no rows.

        Raises:
            StoreError: If the RPC returns an error field.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(type_, "type_")
        validate_non_empty(metadata, "metadata")
        rows = self._callproc("credits_add", [user_id, amount, type_, metadata, bucket, idempotency_key])
        if not rows:
            return None
        return AddCreditsRow.model_validate(rows[0])

    def get_available(self, user_id: str) -> AvailableRow | None:
        """Get the available (unreserved) credit balance for a user.

        Args:
            user_id: The user ID.

        Returns:
            AvailableRow if found, None if the user has no balance.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_available_credits", [user_id])
        if not rows:
            return None
        return AvailableRow.model_validate(rows[0])
