"""Team types — mirrors JS SDK's ``credits/types/teams.ts``."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class Team(BaseModel):
    team_id: str = ""
    name: str = ""
    balance: Decimal = Decimal(0)
    member_count: int = 0
    created_at: str = ""


class TeamBalanceResult(BaseModel):
    team_id: str = ""
    name: str = ""
    balance: Decimal = Decimal(0)
    member_count: int = 0


class TeamMember(BaseModel):
    user_id: str = ""
    role: str = ""
    spend_cap: Decimal | None = None
    total_spent: Decimal = Decimal(0)


class CreateTeamResult(BaseModel):
    team_id: str = ""
    name: str = ""


class AddTeamMemberResult(BaseModel):
    team_id: str = ""
    user_id: str = ""
    role: str = "member"


class TeamDeductionResult(BaseModel):
    entry_id: str = ""
    team_id: str = ""
    user_id: str = ""
    amount: Decimal = Decimal(0)
    team_balance_after: Decimal = Decimal(0)
    error: str | None = None
