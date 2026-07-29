from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty
from bursar.credits.postgres.repositories.schemas import (
    AllowanceRow,
    PlanMigrationBatchRow,
    PlanMigrationUsersRow,
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

    def __init__(self, callproc: DbQuery, query: DbQuery) -> None:
        self._callproc = callproc
        self._query = query

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
        row = dict(rows[0]) if isinstance(rows[0], dict) else None
        if row is None:
            return None

        credit_policy_type = row.get("credit_policy_type")
        credit_limit = row.get("credit_limit")
        operation_admission = row.get("operation_admission")
        per_operation = {}
        if isinstance(operation_admission, dict):
            per_operation = {
                str(operation): {"max_concurrent": policy.get("max_in_flight")}
                for operation, policy in operation_admission.items()
                if isinstance(policy, dict) and policy.get("max_in_flight") is not None
            }

        reset_unit = row.get("credit_allowance_reset_unit")
        reset_count = row.get("credit_allowance_reset_count")
        reset_anchor = row.get("credit_allowance_reset_anchor")
        allowance_period = "calendar_month"
        if reset_anchor == "rolling" and reset_unit == "day" and reset_count == 30:
            allowance_period = "rolling_30d"
        elif reset_anchor == "plan_assignment":
            allowance_period = "anniversary"

        row.update(
            {
                "allowance_amount": row.get("credit_allowance_amount"),
                "allowance_period": allowance_period,
                "billing_mode": "overdraft" if credit_policy_type == "credit_line" else "strict",
                "overdraft_floor": f"-{credit_limit}"
                if credit_policy_type == "credit_line" and credit_limit is not None
                else None,
                "max_concurrent": row.get("admission_max_in_flight"),
                "per_operation": per_operation,
                "config_version": row.get("catalog_revision_no"),
                "catalog_version": row.get("catalog_revision_no"),
            }
        )
        return UserPlanRow.model_validate(row)

    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        plan_assigned_at: str | None,
    ) -> SetUserPlanRow | None:
        """Assign a plan to a user.

        Args:
            user_id: The user ID.
            plan_id: The plan identifier.
            plan_assigned_at: ISO datetime string for the assignment, or None.

        Returns:
            SetUserPlanRow if successful, None if the RPC returned no rows.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(plan_key, "plan_key")
        plan_rows = self._query(
            """SELECT p.id
               FROM bursar.catalog_plans AS p
               JOIN bursar.catalog_revisions AS cr ON cr.id = p.catalog_revision_id
               WHERE cr.status = 'active'
                 AND (p.id::text = %s OR p.plan_key = %s)
               ORDER BY cr.revision_no DESC
               LIMIT 1""",
            [plan_key, plan_key],
        )
        if not plan_rows or not isinstance(plan_rows[0], dict) or plan_rows[0].get("id") is None:
            raise ValueError(f"unknown active plan {plan_key!r}")
        plan_id = str(plan_rows[0]["id"])
        params = [user_id, plan_id]
        if plan_assigned_at is not None:
            params.append(plan_assigned_at)
        rows = self._callproc("assign_plan", params)
        if not rows or rows[0] is not True:
            return None
        return SetUserPlanRow(
            user_id=user_id,
            plan_id=plan_id,
            plan_assigned_at=plan_assigned_at,
        )

    def unset_user_plan(self, user_id: str) -> UnsetUserPlanRow | None:
        """Remove the plan assignment from a user.

        Args:
            user_id: The user ID.

        Returns:
            UnsetUserPlanRow if successful, None if the user had no plan.
        """
        validate_non_empty(user_id, "user_id")
        self._query(
            """DELETE FROM bursar.account_plan_assignments
               WHERE account_id = bursar.account_for_subject(%s::uuid)""",
            [user_id],
        )
        return UnsetUserPlanRow(user_id=user_id)

    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> str | None:
        """Migrate all users on a given plan key to a new config version.

        Args:
            plan_key: The plan key to migrate users from.
            target_config_version: The target config version, or None.

        Returns:
            MigratePlanRow with migration results, or None on failure.
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

    def migrate_plan_users(
        self,
        plan_key: str,
        target_config_version: int | None = None,
    ) -> PlanMigrationUsersRow:
        validate_non_empty(plan_key, "plan_key")
        rows = self._callproc(
            "migrate_plan_users",
            [plan_key, target_config_version],
        )
        return PlanMigrationUsersRow.model_validate((rows or [{}])[0])

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

    def check_allowance(self, user_id: str, period_start: str | None) -> AllowanceRow | None:
        """Check the remaining plan allowance for a user.

        Args:
            user_id: The user ID.
            period_start: ISO date string for the period start, or None.

        Returns:
            AllowanceRow if found, None if no plan or allowance configured.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._query(
            """SELECT
                 apa.plan_id,
                 aw.allowance - aw.consumed - aw.reserved AS allowance_remaining,
                 aw.window_start AS period_start,
                 aw.window_end AS period_end
               FROM bursar.allowance_windows AS aw
               JOIN bursar.credit_accounts AS ca ON ca.id = aw.account_id
               JOIN bursar.account_plan_assignments AS apa ON apa.account_id = ca.id
               WHERE ca.subject_id = %s::uuid
                 AND ca.account_kind = 'personal'
                 AND aw.feature = '__included_credits__'
                 AND (%s::timestamptz IS NULL OR aw.window_start = %s::timestamptz)
               ORDER BY aw.window_end DESC
               LIMIT 1""",
            [user_id, period_start, period_start],
        )
        if not rows:
            return None
        return AllowanceRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def list_quota_events(
        self,
        user_id: str,
        after: str | None,
        limit: int,
        idempotency_key: str | None,
    ) -> list[QuotaEventRow]:
        validate_non_empty(user_id, "user_id")
        rows = self._callproc(
            "list_subject_quota_events",
            [user_id, after, limit, idempotency_key],
        )
        return [QuotaEventRow.model_validate(row) for row in rows if isinstance(row, dict)]
