from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import BucketEnvelopeRow, SweepRow

CallProc = Callable[[str, list[Any]], list[Any]]


class BucketRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def get_bucket_balances(self, user_id: str) -> BucketEnvelopeRow:
        rows = self._callproc("get_user_credit_buckets", [user_id])
        return BucketEnvelopeRow.model_validate(rows[0] if rows else {})

    def sweep_expired_credits(self, dry_run: bool, user_id: str | None) -> SweepRow:
        rows = self._callproc("expire_credits", [dry_run, user_id])
        return SweepRow.model_validate(rows[0] if rows else {})
