from __future__ import annotations

from bursar.repositories._types import CallProc
from bursar.repositories._utils import validate_non_empty
from bursar.repositories.schemas import (
    AllowanceRow,
    CapCheckRow,
    FeatureLimitRow,
    MigratePlanRow,
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

    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def get_user_plan(self, user_id: str) -> UserPlanRow | None:
        """Get the current plan for a user.

        Args:
            user_id: The user ID.

        Returns:
            UserPlanRow if found, None if the user has no plan assigned.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("get_user_plan", [user_id])
        if not rows:
            return None
        return UserPlanRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def set_user_plan(
        self,
        user_id: str,
        plan_id: str,
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
        rows = self._callproc("set_user_plan", [user_id, plan_id, plan_assigned_at])
        if not rows:
            return None
        return SetUserPlanRow.model_validate(rows[0])

    def unset_user_plan(self, user_id: str) -> UnsetUserPlanRow | None:
        """Remove the plan assignment from a user.

        Args:
            user_id: The user ID.

        Returns:
            UnsetUserPlanRow if successful, None if the user had no plan.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("unset_user_plan", [user_id])
        if not rows:
            return None
        return UnsetUserPlanRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def migrate_plan_users(
        self,
        plan_key: str,
        target_config_version: int | None,
    ) -> MigratePlanRow | None:
        """Migrate all users on a given plan key to a new config version.

        Args:
            plan_key: The plan key to migrate users from.
            target_config_version: The target config version, or None.

        Returns:
            MigratePlanRow with migration results, or None on failure.
        """
        validate_non_empty(plan_key, "plan_key")
        rows = self._callproc("migrate_plan_users", [plan_key, target_config_version])
        if not rows:
            return None
        return MigratePlanRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def check_allowance(self, user_id: str, period_start: str | None) -> AllowanceRow | None:
        """Check the remaining plan allowance for a user.

        Args:
            user_id: The user ID.
            period_start: ISO date string for the period start, or None.

        Returns:
            AllowanceRow if found, None if no plan or allowance configured.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("check_plan_allowance", [user_id, period_start])
        if not rows:
            return None
        return AllowanceRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def increment_usage_window(self, user_id: str, plan_id: str, amount: str) -> None:
        """Increment the usage counter for a user's plan window.

        Args:
            user_id: The user ID.
            plan_id: The plan ID.
            amount: The amount to increment as a string (Decimal-safe).
        """
        validate_non_empty(user_id, "user_id")
        self._callproc("increment_usage_window", [user_id, plan_id, amount])

    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: str,
        period_end: str,
    ) -> FeatureLimitRow | None:
        """Check if a user has exceeded a per-feature invocation limit.

        Args:
            user_id: The user ID.
            feature: The feature key to check.
            max_calls: The maximum allowed calls in the period.
            period_start: ISO date string for the window start.
            period_end: ISO date string for the window end.

        Returns:
            FeatureLimitRow if found, None if no limit record exists.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(feature, "feature")
        rows = self._callproc(
            "check_feature_limit",
            [
                user_id,
                feature,
                max_calls,
                period_start,
                period_end,
            ],
        )
        if not rows:
            return None
        return FeatureLimitRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None,
        amount: str,
    ) -> CapCheckRow | None:
        """Check if a proposed spend would exceed a user's spend cap.

        Args:
            user_id: The user ID.
            model: The model being used, or None.
            amount: The proposed spend amount as a string (Decimal-safe).

        Returns:
            CapCheckRow if a cap is configured, None otherwise.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._callproc("check_spend_cap", [user_id, model, amount])
        if not rows:
            return None
        return CapCheckRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None
