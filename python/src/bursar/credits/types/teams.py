"""Team types — mirrors JS SDK's ``credits/types/teams.ts``."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class Team(BaseModel):
    id: str
    name: str
    balance: Decimal
    member_count: int
    created_at: str


class TeamBalanceResult(BaseModel):
    team_id: str
    name: str
    balance: Decimal
    member_count: int


class TeamMember(BaseModel):
    user_id: str
    role: str
    spend_cap: Decimal | None = None
    total_spent: Decimal


class CreateTeamResult(BaseModel):
    team_id: str
    name: str


class AddTeamMemberResult(BaseModel):
    team_id: str
    user_id: str
    role: str


class TeamDeductionResult(BaseModel):
    entry_id: str
    team_id: str
    user_id: str
    amount: Decimal
    team_balance_after: Decimal
    error: str | None = None
