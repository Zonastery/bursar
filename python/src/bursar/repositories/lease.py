from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import DeductionRow, LeaseRow, ReleaseRow

CallProc = Callable[[str, list[Any]], list[Any]]


class LeaseRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def create_lease(self, params: dict[str, Any]) -> LeaseRow:
        rows = self._callproc(
            "create_lease",
            [
                params["user_id"],
                params["amount"],
                params["operation_type"],
                params["billing_mode"],
                params["floor"],
                params.get("max_concurrent"),
                params.get("ttl_seconds", 600),
                params.get("model"),
                params.get("overdraft_floor"),
                params.get("metadata", "{}"),
                params.get("period_start"),
                params.get("feature"),
                params.get("feature_max_calls"),
                params.get("feature_action"),
                params.get("feature_period_start"),
                params.get("feature_period_end"),
            ],
        )
        return LeaseRow.model_validate(rows[0] if rows else {})

    def settle_lease(self, params: dict[str, Any]) -> DeductionRow:
        rows = self._callproc(
            "settle_lease",
            [
                params["user_id"],
                params["lease_id"],
                params["amount"],
                params.get("idempotency_key"),
                params.get("min_balance"),
                params.get("model"),
                params.get("metadata", "{}"),
                params.get("skip_allowance", False),
                params.get("period_start"),
                params.get("feature"),
                params.get("feature_max_calls"),
                params.get("feature_action"),
                params.get("feature_period_start"),
                params.get("feature_period_end"),
            ],
        )
        return DeductionRow.model_validate(rows[0] if rows else {})

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseRow:
        rows = self._callproc("release_lease", [user_id, lease_id])
        return ReleaseRow.model_validate(rows[0] if rows else {})

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseRow:
        rows = self._callproc("renew_lease", [user_id, lease_id, ttl_seconds])
        return LeaseRow.model_validate(rows[0] if rows else {})
