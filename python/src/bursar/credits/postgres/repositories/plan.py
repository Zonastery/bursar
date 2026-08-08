from __future__ import annotations

from uuid import UUID

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_mapping_row,
    require_row,
    validate_non_empty,
    validate_row,
)
from bursar.credits.postgres.repositories.schemas import (
    AllowanceRow,
    EntitlementRow,
    PlanMigrationBatchRow,
    QuotaEventRow,
    QuotaStateRow,
    SetUserPlanRow,
    UnsetUserPlanRow,
    UserPlanRow,
)
from bursar.errors import StoreError


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
        row = optional_mapping_row(
            self._callproc("get_subject_plan", [user_id]),
            "PlanRepository.get_user_plan",
        )
        if row is None:
            return None
        return validate_row(UserPlanRow, row, "PlanRepository.get_user_plan")

    def get_entitlement(self, user_id: str, feature: str) -> EntitlementRow | None:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(feature, "feature")
        rows = self._callproc("get_subject_entitlements", [user_id]) or []
        entitlements = [validate_row(EntitlementRow, row, "PlanRepository.get_entitlement") for row in rows]
        return next((row for row in entitlements if row.feature_key == feature), None)

    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        plan_assigned_at: str | None,
    ) -> SetUserPlanRow:
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
        row = require_mapping_row(
            self._callproc("set_subject_plan", [user_id, plan_key, plan_assigned_at]),
            "PlanRepository.set_user_plan",
        )
        return validate_row(
            SetUserPlanRow,
            row,
            "PlanRepository.set_user_plan",
            indeterminate=True,
        )

    def unset_user_plan(self, user_id: str) -> UnsetUserPlanRow:
        """Remove the plan assignment from a user.

        Args:
            user_id: The user ID.

        Returns:
            UnsetUserPlanRow if successful, None if the user had no plan.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("unassign_plan", [user_id, "sdk_unassignment"])
        result = require_row(rows, "PlanRepository.unset_user_plan")
        if type(result) is not bool:
            raise StoreError(
                "PlanRepository.unset_user_plan: expected a boolean result",
                indeterminate=True,
            )
        if not result:
            raise StoreError("PlanRepository.unset_user_plan: unassign_plan returned false")
        return validate_row(
            UnsetUserPlanRow,
            {"user_id": user_id},
            "PlanRepository.unset_user_plan",
        )

    def set_plan_revision_pin(self, user_id: str, pinned: bool) -> bool:
        """Pin or unpin the user's current assignment to its catalog revision."""
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("set_plan_revision_pin", [user_id, pinned])
        result = require_row(rows, "PlanRepository.set_plan_revision_pin")
        if type(result) is not bool:
            raise StoreError("PlanRepository.set_plan_revision_pin: expected a boolean result")
        return result

    def apply_due_plan_changes(self, limit: int) -> int:
        """Apply one bounded batch of renewal-effective plan changes."""
        rows = self._callproc("apply_due_plan_assignment_changes", [limit]) or []
        result = require_row(rows, "PlanRepository.apply_due_plan_changes")
        if isinstance(result, bool) or not isinstance(result, int) or result < 0:
            raise StoreError("PlanRepository.apply_due_plan_changes: expected a non-negative integer")
        return result

    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> str:
        """Start a resumable migration between catalog plan records.

        Args:
            from_plan_id: Source catalog plan ID, or None for all assignments.
            to_plan_id: Target catalog plan ID.

        Returns:
            Migration ID.
        """
        if from_plan_id is not None:
            validate_non_empty(from_plan_id, "from_plan_id")
        validate_non_empty(to_plan_id, "to_plan_id")
        rows = self._callproc("start_plan_migration", [from_plan_id, to_plan_id])
        value = require_row(rows, "PlanRepository.start_plan_migration")
        try:
            return str(UUID(str(value)))
        except (AttributeError, TypeError, ValueError) as error:
            raise StoreError(
                "PlanRepository.start_plan_migration: expected a UUID result",
                indeterminate=True,
            ) from error

    def migrate_plan_batch(
        self,
        migration_id: str,
        batch_size: int = 100,
    ) -> PlanMigrationBatchRow:
        """Advance a plan migration by one bounded batch."""
        validate_non_empty(migration_id, "migration_id")
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        row = require_mapping_row(
            self._callproc("migrate_plan_batch", [migration_id, batch_size]),
            "PlanRepository.migrate_plan_batch",
        )
        return validate_row(
            PlanMigrationBatchRow,
            row,
            "PlanRepository.migrate_plan_batch",
            indeterminate=True,
        )

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
        return [validate_row(QuotaStateRow, row, "PlanRepository.get_quota_state") for row in rows]

    def check_allowance(self, user_id: str) -> AllowanceRow | None:
        """Check the remaining plan allowance for a user.

        Args:
            user_id: The user ID.
        Returns:
            AllowanceRow if found, None if no plan or allowance configured.
        """
        validate_non_empty(user_id, "user_id")
        row = optional_mapping_row(
            self._callproc("get_subject_allowance", [user_id]),
            "PlanRepository.check_allowance",
        )
        if row is None:
            return None
        return validate_row(AllowanceRow, row, "PlanRepository.check_allowance")

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
        return [validate_row(QuotaEventRow, row, "PlanRepository.list_quota_events") for row in rows]
