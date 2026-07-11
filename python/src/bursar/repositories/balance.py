from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import AddCreditsRow, AvailableRow, BalanceRow

CallProc = Callable[[str, list[Any]], list[Any]]


class BalanceRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def get_balance(self, user_id: str) -> BalanceRow | None:
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
    ) -> AddCreditsRow:
        rows = self._callproc("credits_add", [user_id, amount, type_, metadata, bucket, idempotency_key])
        return AddCreditsRow.model_validate(rows[0] if rows else {})

    def get_available(self, user_id: str) -> AvailableRow:
        rows = self._callproc("get_available_credits", [user_id])
        return AvailableRow.model_validate(rows[0] if rows else {})
