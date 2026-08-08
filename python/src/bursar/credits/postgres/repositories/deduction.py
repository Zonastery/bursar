from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_mapping_row,
    validate_amount,
    validate_non_empty,
    validate_row,
)
from bursar.credits.postgres.repositories.schemas import (
    ChargeRpcRow,
    DeductionRow,
    DeductParams,
    RefundRow,
    RefundRpcRow,
    RevokeRow,
    UsageRecordRow,
    UsageRecordRpcRow,
)


class DeductionRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def deduct_with_allowance(self, params: DeductParams) -> DeductionRow:
        validate_non_empty(params.user_id, "user_id")
        validate_amount(params.amount, "amount")
        rows = (
            self._callproc(
                "charge_usage_for_operation",
                [
                    params.user_id,
                    params.operation,
                    params.amount,
                    params.idempotency_key,
                    params.feature,
                    params.model,
                    params.region,
                    params.metadata,
                    params.measures,
                    params.dimensions,
                ],
            )
            or []
        )
        row = validate_row(
            ChargeRpcRow,
            require_mapping_row(rows, "DeductionRepository.deduct_with_allowance"),
            "DeductionRepository.deduct_with_allowance",
            indeterminate=True,
        )
        details = (
            optional_mapping_row(
                self._callproc(
                    "get_credit_operation_details",
                    [params.user_id, row.ledger_entry_id, params.idempotency_key],
                ),
                "DeductionRepository.deduct_with_allowance.details",
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
                "amount": row.charged,
                "allowance_consumed": row.allowance_covered,
                "balance_after": details.get("balance_after") if details is not None else None,
                "bucket_breakdown": details.get("bucket_breakdown") if details is not None else None,
                "idempotent": row.replayed,
                "error": row.error_code,
            },
            "DeductionRepository.deduct_with_allowance",
        )

    def record_usage(self, params: DeductParams) -> UsageRecordRow:
        validate_non_empty(params.user_id, "user_id")
        validate_amount(params.amount, "amount")
        rows = (
            self._callproc(
                "record_usage",
                [
                    params.user_id,
                    params.operation,
                    params.amount,
                    params.idempotency_key,
                    params.feature,
                    params.model,
                    params.region,
                    params.metadata,
                    params.measures,
                    params.dimensions,
                ],
            )
            or []
        )
        row = validate_row(
            UsageRecordRpcRow,
            require_mapping_row(rows, "DeductionRepository.record_usage"),
            "DeductionRepository.record_usage",
            indeterminate=True,
        )
        return validate_row(
            UsageRecordRow,
            {
                "charge_id": row.charge_id,
                "requested": row.requested,
                "replayed": row.replayed,
                "error_code": row.error_code,
            },
            "DeductionRepository.record_usage",
        )

    def refund_credits(
        self,
        entry_id: str,
        amount: str | None,
        idempotency_key: str,
        reason: str | None,
        metadata: str,
    ) -> RefundRow:
        validate_non_empty(entry_id, "entry_id")
        validate_non_empty(idempotency_key, "idempotency_key")
        rows = (
            self._callproc(
                "refund_credit_by_entry",
                [entry_id, amount, idempotency_key, reason, metadata],
            )
            or []
        )
        row = validate_row(
            RefundRpcRow,
            require_mapping_row(rows, "DeductionRepository.refund_credits"),
            "DeductionRepository.refund_credits",
            indeterminate=True,
        )
        return validate_row(
            RefundRow,
            {
                "refund_entry_id": row.entry_id,
                "user_id": row.subject_id,
                "amount": row.amount,
                "new_balance": row.balance_after,
                "bucket_breakdown": None,
                "error": row.error_code,
            },
            "DeductionRepository.refund_credits",
        )

    def revoke_credits_by_entry_type(self, user_id: str, entry_type: str) -> RevokeRow:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(entry_type, "entry_type")
        rows = self._callproc(
            "revoke_subject_credits_by_operation",
            [user_id, entry_type],
        )
        row = require_mapping_row(rows, "DeductionRepository.revoke_credits_by_entry_type")
        return validate_row(
            RevokeRow,
            {
                "user_id": user_id,
                "entry_type": entry_type,
                "revoked": row.get("revoked"),
                "balance_after": row.get("balance_after"),
                "error_code": row.get("error_code"),
            },
            "DeductionRepository.revoke_credits_by_entry_type",
            indeterminate=True,
        )
