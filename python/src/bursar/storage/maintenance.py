"""Bounded, host-invoked maintenance facades."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bursar.shared.diagnostics import persisted_diagnostic_summary

MaintenanceTaskStatus = Literal["completed", "skipped", "unsupported", "failed"]
MaintenanceRunStatus = Literal["completed", "partial", "failed"]
StorageMaintenanceStatus = Literal["completed", "busy", "not_due", "failed"]


class _MaintenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MaintenanceTaskResult(_MaintenanceModel):
    status: MaintenanceTaskStatus
    count: int = Field(ge=0)
    limit: int = Field(ge=1, le=1_000)
    has_more: bool
    reason: str | None = None
    error: str | None = None


MaintenanceTaskName = Literal[
    "expired_leases",
    "expired_credits",
    "due_plan_changes",
    "past_due_grace_periods",
]


class MaintenanceRunResult(_MaintenanceModel):
    status: MaintenanceRunStatus
    count: int = Field(ge=0)
    has_more: bool
    tasks: dict[MaintenanceTaskName, MaintenanceTaskResult]


class MaintenanceRunOptions(_MaintenanceModel):
    limit: int = Field(default=100, strict=True, ge=1, le=1_000)
    now: datetime | None = None

    @field_validator("now")
    @classmethod
    def validate_now(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("now must include a timezone")
        return value


class MaintenanceOperations(_MaintenanceModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    expire_leases: Callable[[int], int] | None = None
    expire_credits: Callable[[int], int] | None = None
    apply_due_plan_changes: Callable[[int], int] | None = None
    expire_past_due_grace_periods: Callable[[datetime], int] | None = None
    past_due_grace_period_limit: int = Field(default=100, strict=True, ge=1, le=1_000)
    past_due_grace_periods_unavailable_reason: str | None = None


class BursarMaintenance:
    """Tenant-scoped maintenance that never schedules itself."""

    def __init__(self, operations: MaintenanceOperations) -> None:
        self._operations = operations

    def run_once(self, options: MaintenanceRunOptions | None = None) -> MaintenanceRunResult:
        effective = options or MaintenanceRunOptions()
        now = effective.now or datetime.now(UTC)
        tasks: dict[MaintenanceTaskName, MaintenanceTaskResult] = {
            "expired_leases": _run_task(self._operations.expire_leases, effective.limit),
            "expired_credits": _run_task(self._operations.expire_credits, effective.limit),
            "due_plan_changes": _run_task(self._operations.apply_due_plan_changes, effective.limit),
            "past_due_grace_periods": (
                _run_time_task(
                    self._operations.expire_past_due_grace_periods,
                    now,
                    self._operations.past_due_grace_period_limit,
                )
                if self._operations.expire_past_due_grace_periods is not None
                else _unavailable_task(
                    self._operations.past_due_grace_period_limit,
                    self._operations.past_due_grace_periods_unavailable_reason,
                )
            ),
        }
        results = list(tasks.values())
        count = sum(task.count for task in results)
        applicable = [task for task in results if task.status in {"completed", "failed"}]
        failed = sum(task.status == "failed" for task in applicable)
        if applicable and failed == len(applicable):
            status: MaintenanceRunStatus = "failed"
        elif failed > 0:
            status = "partial"
        else:
            status = "completed"
        return MaintenanceRunResult(
            status=status,
            count=count,
            has_more=any(task.has_more for task in results),
            tasks=tasks,
        )


class StorageMaintenanceCounts(_MaintenanceModel):
    usage_payloads_purged: int = Field(ge=0)
    record_only_usage_purged: int = Field(ge=0)
    billing_payloads_purged: int = Field(ge=0)
    quota_usage_events_purged: int = Field(ge=0)
    quota_notifications_purged: int = Field(ge=0)
    terminal_leases_compacted: int = Field(ge=0)
    usage_rollups_purged: int = Field(ge=0)
    outbox_events_purged: int = Field(ge=0)


class StorageMaintenanceResult(_MaintenanceModel):
    status: StorageMaintenanceStatus
    count: int = Field(ge=0)
    has_more: bool
    batch_size: int | None = Field(default=None, ge=0)
    counts: StorageMaintenanceCounts
    last_maintenance_at: str | None = None
    next_maintenance_at: str | None = None
    error: str | None = None


class OperatorMaintenanceRunOptions(_MaintenanceModel):
    mode: Literal["if_due", "force"] = "if_due"
    now: datetime | None = None

    @field_validator("now")
    @classmethod
    def validate_now(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("now must include a timezone")
        return value


StoragePartition = Literal["usage_charge_payloads", "billing_event_payloads"]


class PartitionMaintenanceResult(_MaintenanceModel):
    status: Literal["completed", "busy", "failed"]
    parent_table: StoragePartition
    count: int = Field(ge=0)
    partitions_created: int = Field(ge=0)
    partitions_dropped: int = Field(ge=0)
    partition_lock_timeouts: int = Field(ge=0)
    default_partition_has_rows: bool
    has_more: bool
    error: str | None = None


QueryFn = Callable[[str, Sequence[Any] | None], list[dict[str, Any]]]


class BursarOperatorMaintenance:
    """Explicit operator-only storage and partition maintenance entry points."""

    def __init__(self, query: QueryFn) -> None:
        self._query = query

    def run_once(self, options: OperatorMaintenanceRunOptions | None = None) -> StorageMaintenanceResult:
        effective = options or OperatorMaintenanceRunOptions()
        function_name = "run_storage_maintenance" if effective.mode == "force" else "maybe_run_storage_maintenance"
        try:
            payload = _call_json_function(
                self._query,
                function_name,
                [effective.now or datetime.now(UTC)],
            )
            return _storage_maintenance_result(payload)
        except Exception as error:
            return _failed_storage_maintenance(error)

    def run_partition_once(
        self,
        parent_table: StoragePartition,
        *,
        now: datetime | None = None,
    ) -> PartitionMaintenanceResult:
        if parent_table not in {"usage_charge_payloads", "billing_event_payloads"}:
            raise ValueError("parent_table must be a Bursar storage partition parent")
        if now is not None and (now.tzinfo is None or now.utcoffset() is None):
            raise ValueError("now must include a timezone")
        try:
            payload = _call_json_function(
                self._query,
                "run_storage_partition_maintenance",
                [parent_table, now or datetime.now(UTC)],
            )
            return _partition_maintenance_result(parent_table, payload)
        except Exception as error:
            return PartitionMaintenanceResult(
                status="failed",
                parent_table=parent_table,
                count=0,
                partitions_created=0,
                partitions_dropped=0,
                partition_lock_timeouts=0,
                default_partition_has_rows=False,
                has_more=True,
                error=persisted_diagnostic_summary(error, "partition_maintenance_failed"),
            )


def _run_task(operation: Callable[[int], int] | None, limit: int) -> MaintenanceTaskResult:
    if operation is None:
        return _unavailable_task(limit)
    try:
        count = operation(limit)
        _validate_count(count, "maintenance task")
        return MaintenanceTaskResult(status="completed", count=count, limit=limit, has_more=count == limit)
    except Exception as error:
        return MaintenanceTaskResult(
            status="failed",
            count=0,
            limit=limit,
            has_more=True,
            error=persisted_diagnostic_summary(error, "maintenance_task_failed"),
        )


def _run_time_task(
    operation: Callable[[datetime], int],
    now: datetime,
    limit: int,
) -> MaintenanceTaskResult:
    try:
        count = operation(now)
        _validate_count(count, "maintenance task")
        return MaintenanceTaskResult(status="completed", count=count, limit=limit, has_more=count == limit)
    except Exception as error:
        return MaintenanceTaskResult(
            status="failed",
            count=0,
            limit=limit,
            has_more=True,
            error=persisted_diagnostic_summary(error, "maintenance_task_failed"),
        )


def _unavailable_task(limit: int, reason: str | None = None) -> MaintenanceTaskResult:
    return MaintenanceTaskResult(
        status="skipped" if reason else "unsupported",
        count=0,
        limit=limit,
        has_more=False,
        reason=reason,
    )


def _call_json_function(query: QueryFn, function_name: str, params: Sequence[Any]) -> dict[str, Any]:
    placeholders = ", ".join(["%s"] * len(params))
    rows = query(
        f"SELECT bursar.{function_name}({placeholders}) AS maintenance_result",
        params,
    )
    if not rows or not isinstance(rows[0], dict):
        raise TypeError("maintenance RPC returned no result")
    raw = rows[0].get("maintenance_result")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    raise TypeError("maintenance RPC returned a malformed result")


def _storage_maintenance_result(payload: dict[str, Any]) -> StorageMaintenanceResult:
    status = payload.get("status")
    if status == "busy":
        return StorageMaintenanceResult(status="busy", count=0, has_more=True, counts=_empty_storage_counts())
    if status == "not_due":
        return StorageMaintenanceResult(
            status="not_due",
            count=0,
            has_more=False,
            counts=_empty_storage_counts(),
            last_maintenance_at=_optional_timestamp(payload, "last_maintenance_at"),
            next_maintenance_at=_optional_timestamp(payload, "next_maintenance_at"),
        )
    if status != "completed":
        raise TypeError("maintenance RPC returned an unknown status")
    counts = StorageMaintenanceCounts(
        usage_payloads_purged=_integer_field(payload, "usage_payloads_purged"),
        record_only_usage_purged=_integer_field(payload, "record_only_usage_purged"),
        billing_payloads_purged=_integer_field(payload, "billing_payloads_purged"),
        quota_usage_events_purged=_integer_field(payload, "quota_usage_events_purged"),
        quota_notifications_purged=_integer_field(payload, "quota_notifications_purged"),
        terminal_leases_compacted=_integer_field(payload, "terminal_leases_compacted"),
        usage_rollups_purged=_integer_field(payload, "usage_rollups_purged"),
        outbox_events_purged=_integer_field(payload, "outbox_events_purged"),
    )
    return StorageMaintenanceResult(
        status="completed",
        count=sum(counts.model_dump().values()),
        has_more=_boolean_field(payload, "has_more"),
        batch_size=_integer_field(payload, "batch_size"),
        counts=counts,
    )


def _partition_maintenance_result(
    parent_table: StoragePartition,
    payload: dict[str, Any],
) -> PartitionMaintenanceResult:
    if payload.get("status") == "busy":
        return PartitionMaintenanceResult(
            status="busy",
            parent_table=parent_table,
            count=0,
            partitions_created=0,
            partitions_dropped=0,
            partition_lock_timeouts=0,
            default_partition_has_rows=False,
            has_more=True,
        )
    if payload.get("status") != "completed" or payload.get("parent_table") != parent_table:
        raise TypeError("partition maintenance RPC returned a malformed result")
    created = _integer_field(payload, "partitions_created")
    dropped = _integer_field(payload, "partitions_dropped")
    return PartitionMaintenanceResult(
        status="completed",
        parent_table=parent_table,
        count=created + dropped,
        partitions_created=created,
        partitions_dropped=dropped,
        partition_lock_timeouts=_integer_field(payload, "partition_lock_timeouts"),
        default_partition_has_rows=_boolean_field(payload, "default_partition_has_rows"),
        has_more=_boolean_field(payload, "has_more"),
    )


def _failed_storage_maintenance(error: BaseException) -> StorageMaintenanceResult:
    return StorageMaintenanceResult(
        status="failed",
        count=0,
        has_more=True,
        counts=_empty_storage_counts(),
        error=persisted_diagnostic_summary(error, "storage_maintenance_failed"),
    )


def _empty_storage_counts() -> StorageMaintenanceCounts:
    return StorageMaintenanceCounts(
        usage_payloads_purged=0,
        record_only_usage_purged=0,
        billing_payloads_purged=0,
        quota_usage_events_purged=0,
        quota_notifications_purged=0,
        terminal_leases_compacted=0,
        usage_rollups_purged=0,
        outbox_events_purged=0,
    )


def _validate_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{name} returned an invalid count")
    return value


def _integer_field(payload: dict[str, Any], key: str) -> int:
    return _validate_count(payload.get(key), f"maintenance RPC field {key}")


def _boolean_field(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"maintenance RPC field {key} must be a boolean")
    return value


def _optional_timestamp(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"maintenance RPC field {key} must be a timestamp string")
    return value
