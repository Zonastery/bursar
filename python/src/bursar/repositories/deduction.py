from __future__ import annotations

from bursar.repositories._types import CallProc
from bursar.repositories._utils import validate_non_empty
from bursar.repositories.schemas import DeductionRow, DeductParams, RefundRow, RevokeRow


class DeductionRepository:
    """Repository for deduction, refund, and revocation operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: CallProc) -> None:
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
        transaction_id: str,
        amount: str | None,
        reason: str | None,
        metadata: str,
    ) -> RefundRow | None:
        """Refund a previous credit transaction, optionally for a partial amount.

        Args:
            transaction_id: The original transaction ID to refund.
            amount: The amount to refund as a string, or None for full refund.
            reason: The refund reason, or None.
            metadata: JSON metadata string.

        Returns:
            RefundRow if successful, None if the RPC returned no rows.
        """
        validate_non_empty(transaction_id, "transaction_id")
        rows = self._callproc("refund_credits", [transaction_id, amount, reason, metadata])
        if not rows:
            return None
        return RefundRow.model_validate(rows[0])

    def revoke_credits_by_tx_type(self, user_id: str, tx_type: str) -> RevokeRow | None:
        """Revoke credits for all transactions of a given type for a user.

        Args:
            user_id: The user ID.
            tx_type: The transaction type to revoke.

        Returns:
            RevokeRow with revocation details, or None if nothing to revoke.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("revoke_credits_by_tx_type", [user_id, tx_type])
        if not rows:
            return None
        return RevokeRow.model_validate(rows[0])
