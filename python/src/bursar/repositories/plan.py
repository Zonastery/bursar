from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import (
    AllowanceRow,
    CapCheckRow,
    FeatureLimitRow,
    MigratePlanRow,
    SetUserPlanRow,
    UserPlanRow,
)

CallProc = Callable[[str, list[Any]], list[Any]]


class PlanRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def get_user_plan(self, user_id: str) -> UserPlanRow | None:
        rows = self._callproc("get_user_plan", [user_id])
        if not rows:
            return None
        return UserPlanRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def set_user_plan(
        self,
        user_id: str,
        plan_id: str,
        plan_assigned_at: str | None,
    ) -> SetUserPlanRow:
        params = [user_id, plan_id, plan_assigned_at] if plan_assigned_at else [user_id, plan_id]
        rows = self._callproc("set_user_plan", params)
        return SetUserPlanRow.model_validate(rows[0] if rows else {})

    def unset_user_plan(self, user_id: str) -> dict[str, Any]:
        rows = self._callproc("unset_user_plan", [user_id])
        return (rows[0] if rows else {}) or {}

    def migrate_plan_users(
        self,
        plan_key: str,
        target_config_version: int | None,
    ) -> MigratePlanRow:
        rows = self._callproc("migrate_plan_users", [plan_key, target_config_version])
        return MigratePlanRow.model_validate(rows[0]) if isinstance(rows[0], dict) else MigratePlanRow()

    def check_allowance(self, user_id: str, period_start: str | None) -> AllowanceRow | None:
        rows = self._callproc("check_plan_allowance", [user_id, period_start])
        if not rows:
            return None
        return AllowanceRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def increment_usage_window(self, user_id: str, plan_id: str, amount: str) -> None:
        self._callproc("increment_usage_window", [user_id, plan_id, amount])

    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: str,
        period_end: str,
    ) -> FeatureLimitRow | None:
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
        rows = self._callproc("check_spend_cap", [user_id, model, amount])
        if not rows:
            return None
        return CapCheckRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None
