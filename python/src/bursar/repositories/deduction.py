from __future__ import annotations

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_amount, validate_non_empty
from bursar.repositories.schemas import DeductionRow, DeductParams, RefundRow, RevokeRow


class DeductionRepository:
    """Repository for deduction, refund, and revocation operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def deduct_with_allowance(self, params: DeductParams) -> DeductionRow | None:
        """Atomically deduct credits with allowance, cap, and floor checks.

        Args:
            params: DeductParams with user_id, amount, idempotency_key,
                min_balance, model, metadata, skip_allowance, period_start,
                and optional feature limit fields.

        Returns:
            DeductionRow if successful, None if the RPC returned no rows.
        """
        validate_non_empty(params.user_id, "user_id")
        validate_amount(params.amount, "amount")
        rows = self._callproc(
            "deduct_with_allowance",
            [
                params.user_id,
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

    def refund_credits(
        self,
        entry_id: str,
        amount: str | None,
        reason: str | None,
        metadata: str,
    ) -> RefundRow | None:
        """Refund a previous ledger entry, optionally for a partial amount.

        Args:
            entry_id: The original ledger entry ID to refund.
            amount: The amount to refund as a string, or None for full refund.
            reason: The refund reason, or None.
            metadata: JSON metadata string.

        Returns:
            RefundRow if successful, None if the RPC returned no rows.
        """
        validate_non_empty(entry_id, "entry_id")
        if amount is not None:
            validate_amount(amount, "amount")
        rows = self._callproc("refund_credits", [entry_id, amount, reason, metadata])
        if not rows:
            return None
        return RefundRow.model_validate(rows[0])

    def revoke_credits_by_entry_type(self, user_id: str, entry_type: str) -> RevokeRow | None:
        """Revoke credits for all transactions of a given type for a user.

        Args:
            user_id: The user ID.
            entry_type: The transaction type to revoke.

        Returns:
            RevokeRow with revocation details, or None if nothing to revoke.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("revoke_credits_by_entry_type", [user_id, entry_type])
        if not rows:
            return None
        return RevokeRow.model_validate(rows[0])
