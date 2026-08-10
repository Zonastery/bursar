"""Team types — mirrors JS SDK's ``credits/types/teams.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

TeamRole = Literal["owner", "admin", "member"]


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
    model_config = ConfigDict(extra="forbid")

    user_id: str
    role: TeamRole
    spend_cap: Decimal | None
    total_spent: Decimal


class CreateTeamResult(BaseModel):
    team_id: str
    name: str
    idempotent: bool


class AddTeamMemberResult(BaseModel):
    team_id: str
    user_id: str
    role: TeamRole


class TeamDeductionResult(BaseModel):
    entry_id: str | None
    team_id: str
    user_id: str
    amount: Decimal
    team_balance_after: Decimal | None
    idempotent: bool
    error: str | None = None
