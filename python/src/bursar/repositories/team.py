from __future__ import annotations

from bursar.repositories._types import CallProc
from bursar.repositories._utils import validate_non_empty
from bursar.repositories.schemas import (
    AddTeamMemberRow,
    CreateTeamRow,
    TeamBalanceRow,
    TeamDeductionRow,
    TeamMemberRow,
)


class TeamRepository:
    """Repository for team/shared balance pool operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def create_team(self, name: str, initial_balance: str) -> CreateTeamRow | None:
        """Create a new team with an initial credit balance.

        Args:
            name: The team name.
            initial_balance: The initial credit balance as a string (Decimal-safe).

        Returns:
            CreateTeamRow if created, None if the RPC returned no rows.
        """
        rows = self._callproc("create_team", [name, initial_balance])
        if not rows:
            return None
        return CreateTeamRow.model_validate(rows[0])

    def get_team_balance(self, team_id: str) -> TeamBalanceRow | None:
        """Get the credit balance and member count for a team.

        Args:
            team_id: The team ID.

        Returns:
            TeamBalanceRow if found, None if the team does not exist.
        """
        validate_non_empty(team_id, "team_id")
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
    ) -> AddTeamMemberRow | None:
        """Add a member to a team with an optional spend cap.

        Args:
            team_id: The team ID.
            user_id: The user ID to add.
            role: The member role (e.g. "member", "admin").
            spend_cap: The spend cap as a string, or None for unlimited.

        Returns:
            AddTeamMemberRow if added, None if the RPC returned no rows.
        """
        validate_non_empty(team_id, "team_id")
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("add_team_member", [team_id, user_id, role, spend_cap])
        if not rows:
            return None
        return AddTeamMemberRow.model_validate(rows[0])

    def get_team_members(self, team_id: str) -> list[TeamMemberRow]:
        """Get all members of a team.

        Args:
            team_id: The team ID.

        Returns:
            List of TeamMemberRow (may be empty).
        """
        validate_non_empty(team_id, "team_id")
        rows = self._callproc("get_team_members", [team_id]) or []
        return [TeamMemberRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: str,
        metadata: str,
    ) -> TeamDeductionRow | None:
        """Deduct credits from a team's balance on behalf of a member.

        Args:
            team_id: The team ID.
            user_id: The user ID making the deduction.
            amount: The amount to deduct as a string (Decimal-safe).
            metadata: JSON metadata string.

        Returns:
            TeamDeductionRow if deducted, None if the RPC returned no rows.
        """
        validate_non_empty(team_id, "team_id")
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("deduct_team", [team_id, user_id, amount, metadata])
        if not rows:
            return None
        return TeamDeductionRow.model_validate(rows[0])
