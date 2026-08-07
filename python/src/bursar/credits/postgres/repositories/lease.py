from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import require_row, validate_amount, validate_non_empty
from bursar.credits.postgres.repositories.schemas import (
    CreateLeaseParams,
    DeductionRow,
    LeasePricingContextRow,
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
        validate_non_empty(params.operation_type, "operation_type")
        validate_non_empty(params.idempotency_key, "idempotency_key")
        rows = (
            self._callproc(
                "create_lease_for_operation",
                [
                    params.user_id,
                    params.operation_type,
                    params.amount,
                    params.idempotency_key,
                    f"{params.ttl_seconds} seconds",
                    params.metadata,
                    params.feature,
                    params.measures,
                    params.dimensions,
                    params.minimum_balance,
                    params.max_concurrent,
                ],
            )
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        lease_rows = (
            self._callproc("get_credit_lease", [params.user_id, row.get("lease_id")])
            if row.get("lease_id") is not None
            else []
        )
        lease = dict(lease_rows[0]) if lease_rows else {}
        row.update(
            {
                "user_id": params.user_id,
                "amount": row.get("reserved_amount"),
                "expires_at": lease.get("expires_at"),
                "minimum_balance": lease.get("minimum_balance"),
                "error": row.get("error_code"),
            }
        )
        return LeaseRow.model_validate(row)

    def settle_lease(self, params: SettleLeaseParams) -> DeductionRow | None:
        validate_non_empty(params.user_id, "user_id")
        validate_non_empty(params.lease_id, "lease_id")
        validate_non_empty(params.idempotency_key, "idempotency_key")
        rows = (
            self._callproc(
                "settle_lease",
                [
                    params.user_id,
                    params.lease_id,
                    params.amount,
                    params.idempotency_key,
                    params.feature,
                    params.model,
                    params.region,
                    params.measures,
                    params.dimensions,
                    params.metadata,
                ],
            )
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        details_rows = self._callproc(
            "get_credit_operation_details",
            [params.user_id, row.get("ledger_entry_id"), params.idempotency_key],
        )
        details = dict(details_rows[0]) if details_rows else {}
        row.update(
            {
                "user_id": params.user_id,
                "entry_id": row.get("ledger_entry_id"),
                "amount": row.get("settled_amount"),
                "allowance_consumed": details.get("allowance_covered"),
                "balance_after": details.get("balance_after"),
                "bucket_breakdown": details.get("bucket_breakdown"),
                "idempotent": row.get("replayed"),
                "error": row.get("error_code"),
            }
        )
        return DeductionRow.model_validate(row)

    def get_pricing_context(self, user_id: str, lease_id: str) -> LeasePricingContextRow | None:
        """Return the immutable catalog and plan references captured by a lease."""
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("get_credit_lease_pricing_context", [user_id, lease_id]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        return LeasePricingContextRow.model_validate(rows[0])

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseRow | None:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("release_lease", [user_id, lease_id]) or []
        result = require_row(rows, "LeaseRepository.release_lease")
        return ReleaseRow.model_validate({"released": result == "released" or result is True})

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseRow | None:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a positive integer")
        rows = self._callproc("renew_lease", [user_id, lease_id, f"{ttl_seconds} seconds"]) or []
        if not rows:
            return None
        row = dict(rows[0])
        lease_rows = (
            self._callproc("get_credit_lease", [user_id, row.get("lease_id")])
            if row.get("lease_id") is not None and row.get("error_code") is None
            else []
        )
        lease = dict(lease_rows[0]) if lease_rows else {}
        row.update(
            {
                "user_id": user_id,
                "amount": row.get("reserved_amount"),
                "expires_at": lease.get("expires_at"),
                "minimum_balance": lease.get("minimum_balance"),
                "error": row.get("error_code"),
            }
        )
        return LeaseRow.model_validate(row)

    def expire_leases(self, limit: int) -> int:
        """Expire a bounded lease batch and release its reservations."""
        rows = self._callproc("expire_leases", [limit]) or []
        return int(require_row(rows, "LeaseRepository.expire_leases"))
