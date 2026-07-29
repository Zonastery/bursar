from __future__ import annotations

from decimal import Decimal

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
                    params.feature or "default",
                    params.amount,
                    params.idempotency_key,
                    params.feature,
                    params.model,
                    None,
                    params.metadata,
                ],
            )
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        row.update(
            {
                "user_id": params.user_id,
                "entry_id": row.get("ledger_entry_id"),
                "amount": row.get("charged"),
                "allowance_consumed": row.get("allowance_covered"),
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
        lots = self._query(
            """SELECT l.id, l.granted - l.consumed AS amount
               FROM bursar.credit_lots l
               JOIN bursar.credit_ledger_entries e ON e.id = l.source_entry_id
               WHERE l.account_id = bursar.account_for_subject(%s::uuid)
                 AND l.consumed < l.granted
                 AND e.operation = %s
               ORDER BY l.priority, l.expires_at NULLS LAST, l.created_at, l.id""",
            [user_id, entry_type],
        )
        amount = Decimal("0")
        for lot in lots:
            lot_amount = Decimal(str(lot.get("amount", 0)))
            if lot_amount <= 0:
                continue
            result = self._callproc(
                "revoke_lot",
                [str(lot["id"]), str(lot["amount"]), f"revoke:{entry_type}:{lot['id']}"],
            )
            row = result[0] if result else {}
            if isinstance(row, dict) and row.get("error_code"):
                raise RuntimeError(str(row["error_code"]))
            amount += lot_amount
        balances = self._query(
            """SELECT balance
               FROM bursar.credit_accounts
               WHERE id = bursar.account_for_subject(%s::uuid)""",
            [user_id],
        )
        return RevokeRow.model_validate(
            {
                "user_id": user_id,
                "amount": amount,
                "new_balance": balances[0].get("balance") if balances else None,
                "bucket": None,
            }
        )
