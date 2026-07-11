from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import DeductionRow, RefundRow, RevokeRow

CallProc = Callable[[str, list[Any]], list[Any]]


class DeductionRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def deduct_with_allowance(
        self,
        user_id: str,
        amount: str,
        idempotency_key: str | None,
        min_balance: str,
        model: str | None,
        metadata: str,
        skip_allowance: bool,
        period_start: str | None,
        feature: str | None,
        feature_max_calls: int | None,
        feature_action: str | None,
        feature_period_start: str | None,
        feature_period_end: str | None,
    ) -> DeductionRow:
        rows = self._callproc(
            "deduct_with_allowance",
            [
                user_id,
                amount,
                idempotency_key,
                min_balance,
                model,
                metadata,
                skip_allowance,
                period_start,
                feature,
                feature_max_calls,
                feature_action,
                feature_period_start,
                feature_period_end,
            ],
        )
        return DeductionRow.model_validate(rows[0] if rows else {})

    def refund_credits(
        self,
        transaction_id: str,
        amount: str | None,
        reason: str | None,
        metadata: str,
    ) -> RefundRow:
        rows = self._callproc("refund_credits", [transaction_id, amount, reason, metadata])
        return RefundRow.model_validate(rows[0] if rows else {})

    def revoke_credits_by_tx_type(self, user_id: str, tx_type: str) -> RevokeRow:
        rows = self._callproc("revoke_credits_by_tx_type", [user_id, tx_type])
        return RevokeRow.model_validate(rows[0] if rows else {})
