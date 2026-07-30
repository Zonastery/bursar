from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_amount, validate_non_empty
from bursar.credits.postgres.repositories.schemas import DeductionRow, DeductParams, RefundRow, RevokeRow


class DeductionRepository:
    def __init__(self, callproc: DbQuery, query: DbQuery) -> None:
        self._callproc = callproc
        self._query = query

    def deduct_with_allowance(self, params: DeductParams) -> DeductionRow | None:
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
        if not rows:
            return None
        row = dict(rows[0])
        details: dict[str, object] = {}
        if row.get("error_code") is None:
            detail_rows = self._callproc(
                "get_credit_operation_details",
                [
                    params.user_id,
                    row.get("ledger_entry_id"),
                    params.idempotency_key,
                ],
            )
            if detail_rows:
                details = dict(detail_rows[0])
        row.update(
            {
                "user_id": params.user_id,
                "entry_id": row.get("ledger_entry_id"),
                "amount": row.get("charged"),
                "allowance_consumed": row.get("allowance_covered"),
                "balance_after": details.get("balance_after", row.get("balance_after")),
                "bucket_breakdown": details.get("bucket_breakdown", row.get("bucket_breakdown")),
                "idempotent": row.get("replayed"),
                "error": row.get("error_code"),
            }
        )
        return DeductionRow.model_validate(row)

    def refund_credits(
        self,
        entry_id: str,
        amount: str | None,
        idempotency_key: str,
        reason: str | None,
        metadata: str,
    ) -> RefundRow | None:
        validate_non_empty(entry_id, "entry_id")
        validate_non_empty(idempotency_key, "idempotency_key")
        rows = (
            self._callproc(
                "refund_credit_by_entry",
                [entry_id, amount, idempotency_key, reason, metadata],
            )
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        row.update(
            {
                "refund_entry_id": row.get("entry_id"),
                "user_id": row.get("subject_id"),
                "new_balance": row.get("balance_after"),
                "error": row.get("error_code"),
            }
        )
        return RefundRow.model_validate(row)

    def revoke_credits_by_entry_type(self, user_id: str, entry_type: str) -> RevokeRow | None:
        rows = self._callproc(
            "revoke_subject_credits_by_operation",
            [user_id, entry_type],
        )
        row = dict(rows[0]) if rows else {}
        return RevokeRow.model_validate(
            {
                "user_id": user_id,
                "amount": row.get("revoked"),
                "new_balance": row.get("balance_after"),
                "bucket": None,
            }
        )
