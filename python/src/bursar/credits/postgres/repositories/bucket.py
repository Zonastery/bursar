from __future__ import annotations

from decimal import Decimal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    require_mapping_row,
    validate_non_empty,
    validate_row,
)
from bursar.credits.postgres.repositories.schemas import BucketBalanceRow, BucketEnvelopeRow, SweepRow


class BucketRepository:
    """Repository for credit bucket and expiry operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def get_bucket_balances(self, user_id: str) -> BucketEnvelopeRow:
        """Get all credit bucket balances for a user.

        Args:
            user_id: The user ID.

        Returns:
            BucketEnvelopeRow if found, None if the user has no buckets.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_credit_bucket_balances", [user_id])
        buckets = [validate_row(BucketBalanceRow, row, "BucketRepository.get_bucket_balances") for row in rows or []]
        total = sum(
            (Decimal(str(row.balance)) for row in buckets),
            Decimal(0),
        )
        return validate_row(
            BucketEnvelopeRow,
            {
                "user_id": user_id,
                "buckets": buckets,
                "total_balance": total,
            },
            "BucketRepository.get_bucket_balances",
        )

    def sweep_expired_credits(
        self,
        dry_run: bool = False,
        user_id: str | None = None,
        limit: int = 100,
    ) -> SweepRow:
        """Expire at most ``limit`` eligible credit lots."""
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._callproc("sweep_expired_lots", [limit, user_id, dry_run])
        row = require_mapping_row(rows, "BucketRepository.sweep_expired_credits")
        return validate_row(
            SweepRow,
            row,
            "BucketRepository.sweep_expired_credits",
            indeterminate=not dry_run,
        )
