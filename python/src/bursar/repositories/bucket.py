from __future__ import annotations

from bursar.repositories._types import CallProc
from bursar.repositories._utils import validate_non_empty
from bursar.repositories.schemas import BucketEnvelopeRow, SweepRow


class BucketRepository:
    """Repository for credit bucket and expiry operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def get_bucket_balances(self, user_id: str) -> BucketEnvelopeRow | None:
        """Get all credit bucket balances for a user.

        Args:
            user_id: The user ID.

        Returns:
            BucketEnvelopeRow if found, None if the user has no buckets.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_user_credit_buckets", [user_id])
        if not rows:
            return None
        return BucketEnvelopeRow.model_validate(rows[0])

    def sweep_expired_credits(self, dry_run: bool, user_id: str | None) -> SweepRow | None:
        """Sweep (expire) credits that have passed their expiry date.

        Args:
            dry_run: If True, report what would be expired without modifying data.
            user_id: If set, only sweep credits for this user; otherwise sweep all.

        Returns:
            SweepRow with expiry results, or None if no credits to sweep.
        """
        rows = self._callproc("expire_credits", [dry_run, user_id])
        if not rows:
            return None
        return SweepRow.model_validate(rows[0])
