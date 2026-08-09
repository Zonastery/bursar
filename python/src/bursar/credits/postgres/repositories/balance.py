from __future__ import annotations

from decimal import Decimal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_mapping_row,
    validate_non_empty,
    validate_row,
)
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
        row = optional_mapping_row(rows, "BalanceRepository.get_balance")
        if row is None:
            return None
        return validate_row(
            BalanceRow,
            {
                "user_id": user_id,
                "balance": row.get("balance"),
                "lifetime_purchased": row.get("lifetime_purchased"),
            },
            "BalanceRepository.get_balance",
        )

    def add_credits(
        self,
        user_id: str,
        amount: str,
        type_: str,
        metadata: str,
        expires_at: str | None,
        bucket: str | None,
        idempotency_key: str,
    ) -> AddCreditsRow:
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
        row = require_mapping_row(rows, "BalanceRepository.add_credits")
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
                if grant:
                    grant_row = grant[0]
                    if isinstance(grant_row, str):
                        resolved_bucket = grant_row
                    elif isinstance(grant_row, dict):
                        resolved_bucket = grant_row.get("bucket_key")
        return validate_row(
            AddCreditsRow,
            {
                "entry_id": row.get("entry_id"),
                "user_id": user_id,
                "amount": amount,
                "new_balance": row.get("balance_after"),
                "lifetime_purchased": lifetime_purchased,
                "bucket": resolved_bucket if amount_dec > 0 else None,
                "idempotent": row.get("replayed"),
                "error": error_code,
            },
            "BalanceRepository.add_credits",
        )

    def get_available(self, user_id: str) -> AvailableRow | None:
        validate_non_empty(user_id, "user_id")
        # Route through get_credit_state so reserved/available reflect active
        # lease holds (get_credit_bucket_balances reported reserved=0 always).
        rows = self._callproc("get_credit_state", [user_id]) or []
        row = optional_mapping_row(rows, "BalanceRepository.get_available")
        if row is None:
            return None
        return validate_row(
            AvailableRow,
            {
                "balance": row.get("balance"),
                "reserved": row.get("reserved"),
                "available": row.get("available"),
            },
            "BalanceRepository.get_available",
        )

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
        return [
            validate_row(GrantProgramAwardRow, row, "BalanceRepository.execute_grant_program") for row in rows or []
        ]
