from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty
from bursar.credits.postgres.repositories.schemas import (
    AllowanceRow,
    PlanMigrationBatchRow,
    QuotaEventRow,
    QuotaStateRow,
    SetUserPlanRow,
    UnsetUserPlanRow,
    UserPlanRow,
)


class PlanRepository:
    """Repository for user plan operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery, query: DbQuery | None = None) -> None:
        self._callproc = callproc
        del query

    def get_user_plan(self, user_id: str) -> UserPlanRow | None:
        """Get the current plan for a user.

        Args:
            user_id: The user ID.

        Returns:
            UserPlanRow if found, None if the user has no plan assigned.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_subject_plan", [user_id])
        if not rows:
            return None
        if not isinstance(rows[0], dict):
            return None
        return UserPlanRow.model_validate(rows[0])

    def get_entitlement(self, user_id: str, feature: str) -> dict[str, object] | None:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(feature, "feature")
        rows = self._callproc("get_subject_entitlements", [user_id])
        return next(
            (row for row in rows if isinstance(row, dict) and row.get("feature_key") == feature),
            None,
        )

    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        plan_assigned_at: str | None,
    ) -> SetUserPlanRow | None:
        """Assign a plan to a user.

        Args:
            user_id: The user ID.
            plan_key: The public plan key.
            plan_assigned_at: ISO datetime string for the assignment, or None.

        Returns:
            SetUserPlanRow if successful, None if the RPC returned no rows.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(plan_key, "plan_key")
        plan_rows = self._callproc("resolve_active_plan", [plan_key])
        if not plan_rows or not isinstance(plan_rows[0], dict) or plan_rows[0].get("id") is None:
            raise ValueError(f"unknown active plan {plan_key!r}")
        plan_id = str(plan_rows[0]["id"])
        params = [user_id, plan_id]
        if plan_assigned_at is not None:
            params.append(plan_assigned_at)
        rows = self._callproc("assign_plan", params)
        if not rows or rows[0] is not True:
            return None
        assigned = self.get_user_plan(user_id)
        return SetUserPlanRow(
            user_id=user_id,
            plan_id=(assigned.plan_id if assigned and assigned.plan_id else plan_id),
            plan_assigned_at=(assigned.plan_assigned_at if assigned is not None else plan_assigned_at),
        )

    def unset_user_plan(self, user_id: str) -> UnsetUserPlanRow | None:
        """Remove the plan assignment from a user.

        Args:
            user_id: The user ID.

        Returns:
            UnsetUserPlanRow if successful, None if the user had no plan.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("unassign_plan", [user_id, "sdk_unassignment"])
        if not rows or rows[0] is not True:
            return None
        return UnsetUserPlanRow(user_id=user_id)

    def set_plan_revision_pin(self, user_id: str, pinned: bool) -> bool:
        """Pin or unpin the user's current assignment to its catalog revision."""
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("set_plan_revision_pin", [user_id, pinned])
        return bool(rows and rows[0] is True)

    def apply_due_plan_changes(self, limit: int) -> int:
        """Apply one bounded batch of renewal-effective plan changes."""
        rows = self._callproc("apply_due_plan_assignment_changes", [limit]) or []
        return int(rows[0]) if rows else 0

    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> str | None:
        """Start a resumable migration between catalog plan records.

        Args:
            from_plan_id: Source catalog plan ID, or None for all assignments.
            to_plan_id: Target catalog plan ID.

        Returns:
            Migration ID, or None when the plan IDs are invalid.
        """
        if from_plan_id is not None:
            validate_non_empty(from_plan_id, "from_plan_id")
        validate_non_empty(to_plan_id, "to_plan_id")
        rows = self._callproc("start_plan_migration", [from_plan_id, to_plan_id])
        return str(rows[0]) if rows and rows[0] is not None else None

    def migrate_plan_batch(
        self,
        migration_id: str,
        batch_size: int = 100,
    ) -> PlanMigrationBatchRow | None:
        """Advance a plan migration by one bounded batch."""
        validate_non_empty(migration_id, "migration_id")
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        rows = self._callproc("migrate_plan_batch", [migration_id, batch_size])
        if not rows or not isinstance(rows[0], dict):
            return None
        return PlanMigrationBatchRow.model_validate(rows[0])

    def get_quota_state(
        self,
        user_id: str,
        quota_key: str | None = None,
    ) -> list[QuotaStateRow]:
        validate_non_empty(user_id, "user_id")
        rows = self._callproc(
            "get_subject_quota_state",
            [user_id, quota_key],
        )
        return [QuotaStateRow.model_validate(row) for row in rows if isinstance(row, dict)]

    def check_allowance(self, user_id: str) -> AllowanceRow | None:
        """Check the remaining plan allowance for a user.

        Args:
            user_id: The user ID.
        Returns:
            AllowanceRow if found, None if no plan or allowance configured.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_subject_allowance", [user_id])
        if not rows:
            return None
        return AllowanceRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def list_quota_events(
        self,
        user_id: str,
        after: str | None,
        limit: int,
        idempotency_key: str | None,
        after_id: str | None,
    ) -> list[QuotaEventRow]:
        validate_non_empty(user_id, "user_id")
        rows = self._callproc(
            "list_subject_quota_events",
            [user_id, after, limit, idempotency_key, after_id],
        )
        return [QuotaEventRow.model_validate(row) for row in rows if isinstance(row, dict)]
