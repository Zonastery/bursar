from __future__ import annotations

from decimal import Decimal

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_mapping_row,
    require_row,
    validate_amount,
    validate_non_empty,
    validate_row,
)
from bursar.credits.postgres.repositories.schemas import (
    AddTeamMemberRow,
    CreateTeamRow,
    TeamBalanceRow,
    TeamDeductionRow,
    TeamDeductionRpcRow,
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
    ) -> CreateTeamRow:
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
        row = require_mapping_row(
            self._callproc("create_team", [owner_subject_id, name, initial_balance]),
            "TeamRepository.create_team",
        )
        return validate_row(CreateTeamRow, row, "TeamRepository.create_team", indeterminate=True)

    def get_team_balance(self, team_id: str) -> TeamBalanceRow | None:
        """Get the credit balance and member count for a team.

        Args:
            team_id: The team ID.

        Returns:
            TeamBalanceRow if found, None if the team does not exist.
        """
        validate_non_empty(team_id, "team_id")
        row = optional_mapping_row(
            self._callproc("get_team_balance", [team_id]),
            "TeamRepository.get_team_balance",
        )
        if row is None:
            return None
        return validate_row(TeamBalanceRow, row, "TeamRepository.get_team_balance")

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
        result = require_row(rows, "TeamRepository.add_team_member")
        if type(result) is not bool:
            raise StoreError(
                "TeamRepository.add_team_member: expected a boolean result",
                indeterminate=True,
            )
        if not result:
            raise StoreError("TeamRepository.add_team_member: set_team_member returned false")
        return validate_row(
            AddTeamMemberRow,
            {"team_id": team_id, "user_id": user_id, "role": role},
            "TeamRepository.add_team_member",
        )

    def get_team_members(self, team_id: str) -> list[TeamMemberRow]:
        """Get all members of a team.

        Args:
            team_id: The team ID.

        Returns:
            List of TeamMemberRow (may be empty).
        """
        validate_non_empty(team_id, "team_id")
        rows = self._callproc("list_team_members", [team_id]) or []
        return [validate_row(TeamMemberRow, row, "TeamRepository.get_team_members") for row in rows]

    def remove_team_member(self, team_id: str, user_id: str) -> bool:
        """Remove a member unless they are the team's final owner."""
        validate_non_empty(team_id, "team_id")
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("remove_team_member", [team_id, user_id]) or []
        result = require_row(rows, "TeamRepository.remove_team_member")
        if type(result) is not bool:
            raise StoreError("TeamRepository.remove_team_member: expected a boolean result")
        return result

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: str,
        idempotency_key: str,
        operation: str,
        metadata: str,
    ) -> TeamDeductionRow:
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
        row = validate_row(
            TeamDeductionRpcRow,
            require_mapping_row(rows, "TeamRepository.deduct_team"),
            "TeamRepository.deduct_team",
            indeterminate=True,
        )
        if row.error_code is None and Decimal(str(row.amount)) != Decimal(amount):
            raise StoreError(
                "TeamRepository.deduct_team: committed amount differs from the request",
                indeterminate=True,
            )
        return validate_row(
            TeamDeductionRow,
            {
                "entry_id": row.entry_id,
                "team_id": row.team_id,
                "user_id": row.subject_id,
                "amount": amount,
                "team_balance_after": row.balance_after,
                "replayed": row.replayed,
                "error": row.error_code,
            },
            "TeamRepository.deduct_team",
        )
