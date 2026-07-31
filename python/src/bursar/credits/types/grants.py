"""Grant-program types — mirrors JS SDK's ``credits/types/grants.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

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

    grant_event_id: str | None
    grant_award_id: str | None
    recipient_subject_id: str | None
    ledger_entry_id: str | None
    amount: Decimal
    replayed: bool
    error: str | None = None
