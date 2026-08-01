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
        # get_credit_state yields balance (net of expired lots), reserved,
        # available, and lifetime_purchased. get_credit_bucket_balances has no
        # lifetime_purchased column, so the fresh-grant/replace-prior logic in the
        # service layer used to read a field that was always 0 (JS parity).
        rows = self._callproc("get_credit_state", [user_id]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        return BalanceRow.model_validate({"user_id": user_id, **rows[0]})

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
        amount_dec = Decimal(amount)
        entry_kind = "adjustment" if amount_dec < 0 else "purchase" if type_ == "purchase" else "grant"
        rows = (
            self._callproc(
                "post_credit",
                [
                    user_id,
                    entry_kind,
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
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        error_code = row.get("error_code")
        lifetime_purchased = None
        resolved_bucket = None
        if error_code is None:
            # Mirror the JS store: post_credit's result carries neither the new
            # lifetime_purchased nor the bucket the grant actually landed in, so
            # fetch both — otherwise results carry placeholders (0 / "default").
            state = self._callproc("get_credit_state", [user_id]) or []
            if state and isinstance(state[0], dict):
                lifetime_purchased = state[0].get("lifetime_purchased")
            entry_id = row.get("entry_id")
            if amount_dec > 0 and entry_id is not None:
                grant = self._callproc("get_credit_grant_details", [user_id, entry_id]) or []
                if grant and isinstance(grant[0], dict):
                    resolved_bucket = grant[0].get("bucket_key")
        row.update(
            {
                "user_id": user_id,
                "amount": amount,
                "new_balance": row.get("balance_after"),
                "lifetime_purchased": lifetime_purchased,
                "bucket": resolved_bucket or bucket or "default",
                "idempotent": row.get("replayed"),
                "error": error_code,
            }
        )
        return AddCreditsRow.model_validate(row)

    def get_available(self, user_id: str) -> AvailableRow | None:
        validate_non_empty(user_id, "user_id")
        # Route through get_credit_state so reserved/available reflect active
        # lease holds (get_credit_bucket_balances reported reserved=0 always).
        rows = self._callproc("get_credit_state", [user_id]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        return AvailableRow.model_validate(rows[0])

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
