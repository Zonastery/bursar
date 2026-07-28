from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_amount, validate_non_empty
from bursar.credits.postgres.repositories.schemas import (
    CreateLeaseParams,
    DeductionRow,
    LeaseRow,
    ReleaseRow,
    SettleLeaseParams,
)


class LeaseRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def create_lease(self, params: CreateLeaseParams) -> LeaseRow | None:
        validate_non_empty(params.user_id, "user_id")
        validate_amount(params.amount, "amount")
        rows = (
            self._callproc(
                "create_lease_for_operation",
                [
                    params.user_id,
                    params.amount,
                    params.operation_type,
                    params.feature,
                    params.feature_max_calls or 1,
                    f"{params.ttl_seconds} seconds",
                    params.metadata,
                ],
            )
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        row.update({"user_id": params.user_id, "amount": row.get("reserved_amount"), "error": row.get("error_code")})
        return LeaseRow.model_validate(row)

    def settle_lease(self, params: SettleLeaseParams) -> DeductionRow | None:
        validate_non_empty(params.user_id, "user_id")
        validate_non_empty(params.lease_id, "lease_id")
        rows = (
            self._callproc("settle_lease", [params.user_id, params.lease_id, params.amount, params.idempotency_key])
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        row.update(
            {
                "entry_id": row.get("ledger_entry_id"),
                "amount": row.get("settled_amount"),
                "idempotent": row.get("replayed"),
                "error": row.get("error_code"),
            }
        )
        return DeductionRow.model_validate(row)

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseRow | None:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("release_lease", [user_id, lease_id]) or []
        return ReleaseRow.model_validate({"released": bool(rows and (rows[0] == "released" or rows[0] is True))})
