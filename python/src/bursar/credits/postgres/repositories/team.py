from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import require_row, validate_amount, validate_non_empty
from bursar.credits.postgres.repositories.schemas import (
    AddTeamMemberRow,
    CreateTeamRow,
    TeamBalanceRow,
    TeamDeductionRow,
    TeamMemberRow,
)
from bursar.errors import StoreError


class TeamRepository:
    """Repository for team/shared balance pool operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def create_team(
        self,
        owner_subject_id: str,
        name: str,
        initial_balance: str,
    ) -> CreateTeamRow | None:
        """Create a new team with an initial credit balance.

        Args:
            name: The team name.
            initial_balance: The initial credit balance as a string (Decimal-safe).

        Returns:
            CreateTeamRow if created, None if the RPC returned no rows.
        """
        validate_non_empty(name, "name")
        validate_amount(initial_balance, "initial_balance")
        validate_non_empty(owner_subject_id, "owner_subject_id")
        rows = self._callproc("create_team", [owner_subject_id, name, initial_balance])
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
    ) -> AddTeamMemberRow:
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
        rows = self._callproc("set_team_member", [team_id, user_id, role, spend_cap])
        if require_row(rows, "TeamRepository.add_team_member") is not True:
            raise StoreError("TeamRepository.add_team_member: set_team_member returned false")
        return AddTeamMemberRow(team_id=team_id, user_id=user_id, role=role)

    def get_team_members(self, team_id: str) -> list[TeamMemberRow]:
        """Get all members of a team.

        Args:
            team_id: The team ID.

        Returns:
            List of TeamMemberRow (may be empty).
        """
        validate_non_empty(team_id, "team_id")
        rows = self._callproc("list_team_members", [team_id]) or []
        members: list[TeamMemberRow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # list_team_members RETURNS a `user_id` column already; the old
            # pop("subject_id") remap found nothing and clobbered it with "".
            members.append(TeamMemberRow.model_validate(row))
        return members

    def remove_team_member(self, team_id: str, user_id: str) -> bool:
        """Remove a member unless they are the team's final owner."""
        validate_non_empty(team_id, "team_id")
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("remove_team_member", [team_id, user_id]) or []
        return require_row(rows, "TeamRepository.remove_team_member") is True

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: str,
        idempotency_key: str,
        operation: str,
        metadata: str,
    ) -> TeamDeductionRow | None:
        """Deduct credits from a team's balance on behalf of a member.

        Args:
            team_id: The team ID.
            user_id: The user ID making the deduction.
            amount: The amount to deduct as a string (Decimal-safe).
            idempotency_key: Replay key stored on the charge (dedupe key).
            operation: The operation label recorded on the charge.
            metadata: JSON metadata string.

        Returns:
            TeamDeductionRow if deducted, None if the RPC returned no rows.
        """
        validate_non_empty(team_id, "team_id")
        validate_non_empty(user_id, "user_id")
        validate_amount(amount, "amount")
        validate_non_empty(idempotency_key, "idempotency_key")
        validate_non_empty(operation, "operation")
        rows = self._callproc(
            "deduct_team",
            [team_id, user_id, amount, idempotency_key, operation, metadata],
        )
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        # deduct_team RETURNS subject_id / balance_after / error_code; map them to
        # the TeamDeductionRow field names (mirrors the JS repo remap).
        return TeamDeductionRow.model_validate(
            {
                **row,
                "user_id": row.get("subject_id", ""),
                "team_balance_after": row.get("balance_after"),
                "error": row.get("error_code"),
            }
        )
