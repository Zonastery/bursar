from __future__ import annotations

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_amount, validate_non_empty
from bursar.repositories.schemas import CreateLeaseParams, DeductionRow, LeaseRow, ReleaseRow, SettleLeaseParams


class LeaseRepository:
    """Repository for lease lifecycle operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def create_lease(self, params: CreateLeaseParams) -> LeaseRow | None:
        """Create a credit lease (reservation) for a user.

        Args:
            params: CreateLeaseParams with user_id, amount, operation_type,
                billing_mode, floor, max_concurrent, ttl_seconds, model,
                overdraft_floor, metadata, period_start, and optional
                feature limit fields.

        Returns:
            LeaseRow if created, None if the RPC returned no rows.
        """
        validate_non_empty(params.user_id, "user_id")
        validate_amount(params.amount, "amount")
        rows = self._callproc(
            "create_lease",
            [
                params.user_id,
                params.amount,
                params.operation_type,
                params.billing_mode,
                params.floor,
                params.max_concurrent,
                params.ttl_seconds,
                params.model,
                params.overdraft_floor,
                params.metadata,
                params.period_start,
                params.feature,
                params.feature_max_calls,
                params.feature_action,
                params.feature_period_start,
                params.feature_period_end,
            ],
        )
        if not rows:
            return None
        return LeaseRow.model_validate(rows[0])

    def settle_lease(self, params: SettleLeaseParams) -> DeductionRow | None:
        """Settle a lease by deducting the actual amount used.

        Args:
            params: SettleLeaseParams with user_id, lease_id, amount,
                idempotency_key, min_balance, model, metadata,
                skip_allowance, period_start, and optional feature limit fields.

        Returns:
            DeductionRow if settled, None if the RPC returned no rows.
        """
        validate_non_empty(params.user_id, "user_id")
        validate_non_empty(params.lease_id, "lease_id")
        rows = self._callproc(
            "settle_lease",
            [
                params.user_id,
                params.lease_id,
                params.amount,
                params.idempotency_key,
                params.min_balance,
                params.model,
                params.metadata,
                params.skip_allowance,
                params.period_start,
                params.feature,
                params.feature_max_calls,
                params.feature_action,
                params.feature_period_start,
                params.feature_period_end,
            ],
        )
        if not rows:
            return None
        return DeductionRow.model_validate(rows[0])

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseRow | None:
        """Release a lease without deducting credits (cancels the reservation).

        Args:
            user_id: The user ID.
            lease_id: The lease ID to release.

        Returns:
            ReleaseRow if released, None if the lease was not found.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("release_lease", [user_id, lease_id])
        if not rows:
            return None
        return ReleaseRow.model_validate(rows[0])

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseRow | None:
        """Extend the TTL of an existing lease.

        Args:
            user_id: The user ID.
            lease_id: The lease ID to renew.
            ttl_seconds: The new TTL in seconds from now.

        Returns:
            LeaseRow with updated expiry, or None if the lease was not found.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("renew_lease", [user_id, lease_id, ttl_seconds])
        if not rows:
            return None
        return LeaseRow.model_validate(rows[0])
