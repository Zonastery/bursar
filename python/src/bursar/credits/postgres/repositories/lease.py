from __future__ import annotations

from decimal import Decimal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_mapping_row,
    require_row,
    validate_amount,
    validate_non_empty,
    validate_row,
)
from bursar.credits.postgres.repositories.schemas import (
    CreateLeaseParams,
    DeductionRow,
    LeaseMutationRpcRow,
    LeasePricingContextRow,
    LeaseRow,
    ReleaseRow,
    SettleLeaseParams,
    SettleLeaseRpcRow,
)
from bursar.errors import StoreError


class LeaseRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def create_lease(self, params: CreateLeaseParams) -> LeaseRow:
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
        row = validate_row(
            LeaseMutationRpcRow,
            require_mapping_row(rows, "LeaseRepository.create_lease"),
            "LeaseRepository.create_lease",
            indeterminate=True,
        )
        if row.error_code is not None:
            return validate_row(
                LeaseRow,
                {
                    "lease_id": None,
                    "user_id": params.user_id,
                    "amount": None,
                    "expires_at": None,
                    "minimum_balance": None,
                    "error": row.error_code,
                },
                "LeaseRepository.create_lease",
            )
        lease = optional_mapping_row(
            self._callproc("get_credit_lease", [params.user_id, row.lease_id]),
            "LeaseRepository.create_lease.details",
        )
        return validate_row(
            LeaseRow,
            {
                "lease_id": row.lease_id,
                "user_id": params.user_id,
                "amount": row.reserved_amount,
                "expires_at": lease.get("expires_at") if lease is not None else None,
                "minimum_balance": lease.get("minimum_balance") if lease is not None else None,
                "error": None,
            },
            "LeaseRepository.create_lease",
        )

    def settle_lease(self, params: SettleLeaseParams) -> DeductionRow:
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
        row = validate_row(
            SettleLeaseRpcRow,
            require_mapping_row(rows, "LeaseRepository.settle_lease"),
            "LeaseRepository.settle_lease",
            indeterminate=True,
        )
        if row.error_code is None and Decimal(str(row.settled_amount)) != Decimal(params.amount):
            raise StoreError(
                "LeaseRepository.settle_lease: committed amount differs from the request",
                indeterminate=True,
            )
        details = (
            optional_mapping_row(
                self._callproc(
                    "get_credit_operation_details",
                    [params.user_id, row.ledger_entry_id, params.idempotency_key],
                ),
                "LeaseRepository.settle_lease.details",
            )
            if row.error_code is None
            else None
        )
        return validate_row(
            DeductionRow,
            {
                "user_id": params.user_id,
                "charge_id": row.charge_id,
                "entry_id": row.ledger_entry_id,
                "amount": row.settled_amount if row.error_code is None else params.amount,
                "allowance_consumed": (
                    details.get("allowance_covered") if row.error_code is None and details is not None else "0"
                ),
                "balance_after": (
                    details.get("balance_after") if row.error_code is None and details is not None else None
                ),
                "bucket_breakdown": (
                    details.get("bucket_breakdown") if row.error_code is None and details is not None else None
                ),
                "idempotent": row.replayed,
                "error": row.error_code,
            },
            "LeaseRepository.settle_lease",
        )

    def get_pricing_context(self, user_id: str, lease_id: str) -> LeasePricingContextRow | None:
        """Return the immutable catalog and plan references captured by a lease."""
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("get_credit_lease_pricing_context", [user_id, lease_id]) or []
        row = optional_mapping_row(rows, "LeaseRepository.get_pricing_context")
        if row is None:
            return None
        return validate_row(LeasePricingContextRow, row, "LeaseRepository.get_pricing_context")

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseRow:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        rows = self._callproc("release_lease", [user_id, lease_id]) or []
        result = require_row(rows, "LeaseRepository.release_lease")
        statuses = {"active", "settling", "settled", "released", "expired", "missing_lease"}
        if not isinstance(result, str) or result not in statuses:
            raise StoreError("LeaseRepository.release_lease: returned an invalid lease status")
        return validate_row(
            ReleaseRow,
            {
                "released": result == "released",
                "reason": None if result == "released" else result,
            },
            "LeaseRepository.release_lease",
        )

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseRow:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(lease_id, "lease_id")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a positive integer")
        rows = self._callproc("renew_lease", [user_id, lease_id, f"{ttl_seconds} seconds"]) or []
        row = validate_row(
            LeaseMutationRpcRow,
            require_mapping_row(rows, "LeaseRepository.renew_lease"),
            "LeaseRepository.renew_lease",
            indeterminate=True,
        )
        if row.error_code is not None:
            return validate_row(
                LeaseRow,
                {
                    "lease_id": None,
                    "user_id": user_id,
                    "amount": None,
                    "expires_at": None,
                    "minimum_balance": None,
                    "error": row.error_code,
                },
                "LeaseRepository.renew_lease",
            )
        lease = optional_mapping_row(
            self._callproc("get_credit_lease", [user_id, row.lease_id]),
            "LeaseRepository.renew_lease.details",
        )
        return validate_row(
            LeaseRow,
            {
                "lease_id": row.lease_id,
                "user_id": user_id,
                "amount": row.reserved_amount,
                "expires_at": lease.get("expires_at") if lease is not None else None,
                "minimum_balance": lease.get("minimum_balance") if lease is not None else None,
                "error": None,
            },
            "LeaseRepository.renew_lease",
        )

    def expire_leases(self, limit: int) -> int:
        """Expire a bounded lease batch and release its reservations."""
        expired = require_row(
            self._callproc("expire_leases", [limit]),
            "LeaseRepository.expire_leases",
        )
        if isinstance(expired, bool) or not isinstance(expired, int) or expired < 0:
            raise StoreError("LeaseRepository.expire_leases: expected a non-negative integer")
        return expired
