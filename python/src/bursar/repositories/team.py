from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import (
    AddTeamMemberRow,
    CreateTeamRow,
    TeamBalanceRow,
    TeamDeductionRow,
    TeamMemberRow,
)

CallProc = Callable[[str, list[Any]], list[Any]]


class TeamRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def create_team(self, name: str, initial_balance: str) -> CreateTeamRow:
        rows = self._callproc("create_team", [name, initial_balance])
        return CreateTeamRow.model_validate(rows[0] if rows else {})

    def get_team_balance(self, team_id: str) -> TeamBalanceRow | None:
        rows = self._callproc("get_team_balance", [team_id])
        if not rows:
            return None
        return TeamBalanceRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str,
        spend_cap: str | None,
    ) -> AddTeamMemberRow:
        rows = self._callproc("add_team_member", [team_id, user_id, role, spend_cap])
        return AddTeamMemberRow.model_validate(rows[0] if rows else {})

    def get_team_members(self, team_id: str) -> list[TeamMemberRow]:
        rows = self._callproc("get_team_members", [team_id]) or []
        return [TeamMemberRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: str,
        metadata: str,
    ) -> TeamDeductionRow:
        rows = self._callproc("deduct_team", [team_id, user_id, amount, metadata])
        return TeamDeductionRow.model_validate(rows[0] if rows else {})
