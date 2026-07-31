from __future__ import annotations

from decimal import Decimal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty
from bursar.credits.postgres.repositories.schemas import (
    AddCreditsRow,
    AvailableRow,
    BalanceRow,
    GrantProgramAwardRow,
)


class BalanceRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def get_balance(self, user_id: str) -> BalanceRow | None:
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_credit_bucket_balances", [user_id]) or []
        if not rows:
            return None
        total = sum(Decimal(str(row.get("balance", 0) or 0)) for row in rows)
        return BalanceRow.model_validate({"user_id": user_id, "balance": total})

    def add_credits(
        self,
        user_id: str,
        amount: str,
        type_: str,
        metadata: str,
        expires_at: str | None,
        bucket: str | None,
        idempotency_key: str | None,
    ) -> AddCreditsRow | None:
        validate_non_empty(user_id, "user_id")
        rows = (
            self._callproc(
                "post_credit",
                [
                    user_id,
                    "purchase" if type_ == "purchase" else "grant",
                    amount,
                    type_,
                    idempotency_key,
                    metadata,
                    bucket,
                    None,
                    expires_at,
                    "0",
                ],
            )
            or []
        )
        if not rows:
            return None
        row = dict(rows[0])
        row.update(
            {
                "user_id": user_id,
                "amount": amount,
                "new_balance": row.get("balance_after"),
                "bucket": bucket or "default",
                "idempotent": row.get("replayed"),
                "error": row.get("error_code"),
            }
        )
        return AddCreditsRow.model_validate(row)

    def get_available(self, user_id: str) -> AvailableRow | None:
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_credit_bucket_balances", [user_id]) or []
        total = sum(Decimal(str(row.get("balance", 0) or 0)) for row in rows)
        return AvailableRow.model_validate({"balance": total, "reserved": 0, "available": total})

    def execute_grant_program(
        self,
        trigger: str,
        program_key: str,
        subject_id: str,
        event_key: str,
        referrer_subject_id: str | None,
        region: str | None,
        metadata: str,
    ) -> list[GrantProgramAwardRow]:
        """Execute a configured grant-program event and return every award row."""
        validate_non_empty(program_key, "program_key")
        validate_non_empty(subject_id, "subject_id")
        validate_non_empty(event_key, "event_key")
        rows = self._callproc(
            "execute_grant_program",
            [trigger, program_key, subject_id, event_key, referrer_subject_id, region, metadata],
        )
        return [GrantProgramAwardRow.model_validate(row) for row in rows or [] if isinstance(row, dict)]
