from __future__ import annotations

from decimal import Decimal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty
from bursar.credits.postgres.repositories.schemas import BucketEnvelopeRow, SweepRow


class BucketRepository:
    """Repository for credit bucket and expiry operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def get_bucket_balances(self, user_id: str) -> BucketEnvelopeRow | None:
        """Get all credit bucket balances for a user.

        Args:
            user_id: The user ID.

        Returns:
            BucketEnvelopeRow if found, None if the user has no buckets.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_credit_bucket_balances", [user_id])
        if not rows:
            return None
        buckets = [dict(row) for row in rows if isinstance(row, dict)]
        total = sum(
            (Decimal(str(row.get("balance", 0) or 0)) for row in buckets),
            Decimal(0),
        )
        return BucketEnvelopeRow.model_validate(
            {
                "user_id": user_id,
                "buckets": buckets,
                "total_balance": total,
            }
        )

    def sweep_expired_credits(self, limit: int = 100) -> SweepRow:
        """Expire at most ``limit`` eligible credit lots."""
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._callproc("expire_lots", [limit])
        return SweepRow(expired_count=int(rows[0]) if rows else 0)
