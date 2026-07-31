"""Plans config parser — mirrors JS SDK's ``config/parse-plans.ts``."""

from __future__ import annotations

from bursar.config.types import (
    PlanDefinition,
    _validate_identifier,
    _validate_map_keys,
)


def _validate_plan(plan: PlanDefinition) -> PlanDefinition:
    if len(plan.allowed_operations) != len(set(plan.allowed_operations)):
        raise ValueError("plans.*.allowed_operations must not contain duplicates")
    for operation in plan.allowed_operations:
        _validate_identifier(operation, "plans.*.allowed_operations")
    _validate_map_keys(plan.features, "plans.*.features")
    _validate_map_keys(plan.quotas, "plans.*.quotas")
    return plan
