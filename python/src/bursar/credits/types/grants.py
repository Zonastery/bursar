"""Grant-program types — mirrors JS SDK's ``credits/types/grants.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from bursar.credits.types.account import CreditMetadata

GrantProgramTrigger = Literal[
    "account_created",
    "referral_completed",
    "promo_code_redeemed",
    "manual",
]


class ExecuteGrantProgramRequest(BaseModel):
    """One application event that may award one or more catalog grants."""

    trigger: GrantProgramTrigger
    program_key: str
    subject_id: str
    event_key: str
    referrer_subject_id: str | None = None
    region: str | None = None
    metadata: CreditMetadata | None = None


class GrantProgramAwardResult(BaseModel):
    """One award row produced by a grant-program execution."""

    model_config = ConfigDict(extra="forbid")

    grant_event_id: str | None
    grant_award_id: str | None
    recipient_subject_id: str | None
    ledger_entry_id: str | None
    amount: Decimal | None
    replayed: bool
    error: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> GrantProgramAwardResult:
        award = (
            self.grant_event_id,
            self.grant_award_id,
            self.recipient_subject_id,
            self.ledger_entry_id,
            self.amount,
        )
        if self.error is None and any(value is None for value in award):
            raise ValueError("successful grant awards require committed fields")
        if self.error is not None and (any(value is not None for value in award) or self.replayed):
            raise ValueError("failed grant awards cannot expose committed fields")
        return self
