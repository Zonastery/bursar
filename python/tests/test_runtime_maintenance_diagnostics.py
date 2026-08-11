from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from bursar.storage.diagnostics import (
    CatalogRevisionSnapshot,
    CheckDependenciesOptions,
    OutboxStatusSnapshot,
    RuntimeDiagnosticsOperations,
    RuntimeDiagnosticsTracker,
    RuntimeStateInput,
)
from bursar.storage.maintenance import (
    BursarMaintenance,
    BursarOperatorMaintenance,
    MaintenanceOperations,
    MaintenanceRunOptions,
    OperatorMaintenanceRunOptions,
)
from bursar.storage.postgres_repository import PostgresStorageRepository
from bursar.storage.runtime import BursarRuntimeOptions, BursarRuntimeStartOptions, create_bursar_runtime

TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_maintenance_runs_bounded_tasks_and_reports_progress() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    seen_limits: list[int] = []

    def count(limit: int, result: int) -> int:
        seen_limits.append(limit)
        return result

    maintenance = BursarMaintenance(
        MaintenanceOperations(
            expire_leases=lambda limit: count(limit, limit),
            expire_credits=lambda limit: count(limit, 2),
            apply_due_plan_changes=lambda limit: count(limit, 1),
            expire_past_due_grace_periods=lambda effective_now: 3 if effective_now == now else 0,
            past_due_grace_period_limit=100,
        )
    )

    result = maintenance.run_once(MaintenanceRunOptions(limit=7, now=now))

    assert seen_limits == [7, 7, 7]
    assert result.status == "completed"
    assert result.count == 13
    assert result.has_more is True
    assert result.tasks["expired_leases"].has_more is True
    assert result.tasks["past_due_grace_periods"].limit == 100


def test_maintenance_reports_safe_failures_and_unavailable_tasks() -> None:
    def fail(_limit: int) -> int:
        raise RuntimeError("postgresql://user:secret@example.invalid/customer-42")

    maintenance = BursarMaintenance(
        MaintenanceOperations(
            expire_leases=fail,
            apply_due_plan_changes=lambda _limit: 0,
            past_due_grace_periods_unavailable_reason="billing is not configured",
        )
    )

    result = maintenance.run_once()

    assert result.status == "partial"
    assert result.tasks["expired_leases"].error == "maintenance_task_failed:RuntimeError"
    assert "secret" not in (result.tasks["expired_leases"].error or "")
    assert result.tasks["expired_credits"].status == "unsupported"
    assert result.tasks["past_due_grace_periods"].status == "skipped"


def test_maintenance_ignores_unavailable_tasks_when_aggregating_status() -> None:
    healthy = BursarMaintenance(
        MaintenanceOperations(
            expire_leases=lambda _limit: 0,
            expire_credits=lambda _limit: 0,
            apply_due_plan_changes=lambda _limit: 0,
            past_due_grace_periods_unavailable_reason="billing is not configured",
        )
    )
    assert healthy.run_once().status == "completed"

    def fail(_limit: int) -> int:
        raise RuntimeError("private")

    failed = BursarMaintenance(
        MaintenanceOperations(
            expire_leases=fail,
            expire_credits=fail,
            apply_due_plan_changes=fail,
            past_due_grace_periods_unavailable_reason="billing is not configured",
        )
    )
    assert failed.run_once().status == "failed"


def test_maintenance_and_diagnostics_propagate_process_control_exceptions() -> None:
    def interrupt(_limit: int) -> int:
        raise KeyboardInterrupt

    maintenance = BursarMaintenance(MaintenanceOperations(expire_leases=interrupt))
    with pytest.raises(KeyboardInterrupt):
        maintenance.run_once()

    tracker = RuntimeDiagnosticsTracker(
        RuntimeDiagnosticsOperations(
            check_postgres=lambda: (_ for _ in ()).throw(SystemExit(2)),
            get_catalog_revision=lambda: None,
        ),
        worker_configured=False,
    )
    with pytest.raises(SystemExit):
        tracker.check_dependencies(RuntimeStateInput(started=True, closed=False, catalog_loaded=True))


def test_operator_maintenance_is_a_separate_facade() -> None:
    queries: list[str] = []

    def query(sql: str, _params: Any = None) -> list[dict[str, Any]]:
        queries.append(sql)
        if "run_storage_partition_maintenance" in sql:
            payload = {
                "status": "completed",
                "parent_table": "usage_charge_payloads",
                "partitions_created": 1,
                "partitions_dropped": 2,
                "partition_lock_timeouts": 0,
                "default_partition_has_rows": False,
                "has_more": False,
            }
        else:
            payload = {
                "status": "completed",
                "batch_size": 100,
                "has_more": False,
                "usage_payloads_purged": 1,
                "record_only_usage_purged": 2,
                "billing_payloads_purged": 3,
                "quota_usage_events_purged": 4,
                "quota_notifications_purged": 5,
                "terminal_leases_compacted": 6,
                "usage_rollups_purged": 7,
                "outbox_events_purged": 8,
            }
        return [{"maintenance_result": payload}]

    maintenance = BursarOperatorMaintenance(query)

    storage = maintenance.run_once(OperatorMaintenanceRunOptions(mode="force"))
    partition = maintenance.run_partition_once("usage_charge_payloads")

    assert storage.status == "completed"
    assert storage.count == 36
    assert partition.status == "completed"
    assert partition.count == 3
    assert "run_storage_maintenance" in queries[0]
    assert "run_storage_partition_maintenance" in queries[1]


def test_diagnostics_state_is_local_and_active_check_forwards_bound() -> None:
    check_postgres = Mock()
    get_catalog_revision = Mock(return_value=CatalogRevisionSnapshot(id="revision-id", version=4))
    get_outbox_status = Mock(
        return_value=OutboxStatusSnapshot(
            pending_count=2,
            processing_count=1,
            delivered_count=5,
            dead_letter_count=0,
            oldest_pending_at="2026-08-10T00:00:00+00:00",
        )
    )
    tracker = RuntimeDiagnosticsTracker(
        RuntimeDiagnosticsOperations(
            check_postgres=check_postgres,
            get_catalog_revision=get_catalog_revision,
            get_outbox_status=get_outbox_status,
        ),
        worker_configured=True,
    )
    state_input = RuntimeStateInput(started=True, closed=False, catalog_loaded=True)

    state = tracker.state(state_input)

    assert state.ready is False
    assert state.financial_ready is True
    assert state.projection_ready is False
    assert state.degraded is True
    assert state.worker.lifecycle == "not_started"
    check_postgres.assert_not_called()
    get_catalog_revision.assert_not_called()
    get_outbox_status.assert_not_called()

    tracker.mark_worker_started()
    diagnostics = tracker.check_dependencies(
        state_input,
        CheckDependenciesOptions(outbox_limit=7),
    )
    assert diagnostics.ready is True
    assert diagnostics.financial_ready is True
    assert diagnostics.projection_ready is True
    assert diagnostics.degraded is False
    assert diagnostics.catalog.current_revision == 4
    assert diagnostics.outbox.limit == 7
    assert diagnostics.outbox.snapshot is not None
    assert diagnostics.outbox.snapshot.pending_count == 2
    get_outbox_status.assert_called_once_with(7)


def test_diagnostics_separates_projection_degradation_from_financial_readiness() -> None:
    tracker = RuntimeDiagnosticsTracker(
        RuntimeDiagnosticsOperations(
            check_postgres=lambda: None,
            get_catalog_revision=lambda: CatalogRevisionSnapshot(id="revision-id", version=4),
            get_outbox_status=lambda _limit: OutboxStatusSnapshot(
                pending_count=0,
                processing_count=0,
                delivered_count=5,
                dead_letter_count=1,
                oldest_pending_at=None,
            ),
        ),
        worker_configured=True,
    )
    tracker.mark_worker_started()

    diagnostics = tracker.check_dependencies(RuntimeStateInput(started=True, closed=False, catalog_loaded=True))

    assert diagnostics.ready is False
    assert diagnostics.financial_ready is True
    assert diagnostics.projection_ready is False
    assert diagnostics.degraded is True
    assert diagnostics.outbox.snapshot is not None
    assert diagnostics.outbox.snapshot.dead_letter_count == 1

    def fail_outbox(_limit: int) -> OutboxStatusSnapshot:
        raise RuntimeError("private outbox failure")

    unavailable = RuntimeDiagnosticsTracker(
        RuntimeDiagnosticsOperations(
            check_postgres=lambda: None,
            get_catalog_revision=lambda: CatalogRevisionSnapshot(id="revision-id", version=4),
            get_outbox_status=fail_outbox,
        ),
        worker_configured=True,
    )
    unavailable.mark_worker_started()
    failed = unavailable.check_dependencies(RuntimeStateInput(started=True, closed=False, catalog_loaded=True))
    assert failed.financial_ready is True
    assert failed.projection_ready is False
    assert failed.degraded is True
    assert failed.outbox.status == "error"
    assert failed.outbox.error == "outbox_check_failed:RuntimeError"


def test_diagnostics_do_not_expose_active_check_exception_messages() -> None:
    def fail() -> None:
        raise RuntimeError("postgresql://user:secret@example.invalid/customer-42")

    tracker = RuntimeDiagnosticsTracker(
        RuntimeDiagnosticsOperations(
            check_postgres=fail,
            get_catalog_revision=lambda: None,
        ),
        worker_configured=False,
    )

    diagnostics = tracker.check_dependencies(RuntimeStateInput(started=True, closed=False, catalog_loaded=True))

    assert diagnostics.postgres.error == "postgres_check_failed:RuntimeError"
    assert "secret" not in (diagnostics.postgres.error or "")


def test_runtime_validates_projection_schema_before_startup_completes() -> None:
    order: list[str] = []
    clickhouse = SimpleNamespace(
        initialize=lambda: order.append("initialize"),
        check_schema_compatibility=lambda: order.append("check"),
        write_usage=lambda _event, _outbox_event_id: None,
        spend_by_user=lambda _start, _end: [],
        spend_by_model=lambda _start, _end: [],
        top_users=lambda _limit, _start, _end: [],
        daily_spend=lambda _start, _end: [],
        aggregate_stats=lambda _start, _end: None,
    )
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=_Pool(),
            operator_postgres=_Pool(),
            tenant_id=TENANT_ID,
            provider_environment="test",
            clickhouse=clickhouse,
            outbox=False,
        )
    )

    runtime.start(BursarRuntimeStartOptions(load_catalog=False))

    assert order == ["initialize", "check"]
    assert runtime.health().started is True
    runtime.close()


class _Cursor:
    def __init__(self) -> None:
        self.description: object | None = None
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: Any = None) -> None:
        if "SELECT 1 AS bursar_reachable" in sql:
            self.description = object()
            self._rows = [{"bursar_reachable": 1}]
        else:
            self.description = None
            self._rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    autocommit = False

    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commit = Mock()
        self.rollback = Mock()

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self.cursor_instance


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.getconn = Mock(return_value=self.connection)
        self.putconn = Mock()
        self.closeall = Mock()


def test_runtime_owns_facades_and_adapts_options_object_stats(monkeypatch: Any) -> None:
    seen_options: list[dict[str, int]] = []

    def stats(_repository: PostgresStorageRepository, options: dict[str, int]) -> dict[str, object]:
        seen_options.append(options)
        return {
            "pendingCount": 3,
            "processingCount": 0,
            "deliveredCount": 9,
            "deadLetterCount": 1,
            "oldestPendingAt": None,
        }

    monkeypatch.setattr(PostgresStorageRepository, "stats", stats, raising=False)
    pool = _Pool()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pool,
            operator_postgres=_Pool(),
            tenant_id=TENANT_ID,
            provider_environment="test",
        )
    )
    get_active = Mock(return_value=SimpleNamespace(id="revision-id", version=5))
    runtime.bursar.catalog.get_active = get_active

    assert isinstance(runtime.maintenance, BursarMaintenance)
    assert isinstance(runtime.operator_maintenance, BursarOperatorMaintenance)
    runtime.state()
    pool.getconn.assert_not_called()
    get_active.assert_not_called()

    diagnostics = runtime.check_dependencies(CheckDependenciesOptions(outbox_limit=11))

    assert diagnostics.outbox.status == "ok"
    assert diagnostics.outbox.limit == 11
    assert diagnostics.outbox.snapshot is not None
    assert diagnostics.outbox.snapshot.dead_letter_count == 1
    assert seen_options == [{"limit": 11, "outbox_limit": 11}]
    runtime.close()
    pool.closeall.assert_not_called()
