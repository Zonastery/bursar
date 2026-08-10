"""Runtime-local state and active dependency diagnostics."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bursar.shared.diagnostics import persisted_diagnostic_summary
from bursar.storage.outbox_worker import OutboxRunResult

WorkerLifecycle = Literal["not_configured", "not_started", "running", "stopped"]
DependencyStatus = Literal["ok", "error", "skipped", "unsupported"]


class _DiagnosticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkerRunSnapshot(_DiagnosticsModel):
    source: Literal["manual"] = "manual"
    status: Literal["completed", "failed"]
    started_at: str
    completed_at: str
    result: OutboxRunResult | None
    error: str | None = None


class WorkerErrorSnapshot(_DiagnosticsModel):
    at: str
    error: str


class WorkerState(_DiagnosticsModel):
    configured: bool
    lifecycle: WorkerLifecycle
    last_run: WorkerRunSnapshot | None
    last_error: WorkerErrorSnapshot | None


class BursarRuntimeState(_DiagnosticsModel):
    ready: bool
    financial_ready: bool
    projection_ready: bool
    degraded: bool
    started: bool
    closed: bool
    catalog_loaded: bool
    worker: WorkerState


class DependencyCheck(_DiagnosticsModel):
    status: DependencyStatus
    latency_ms: float | None = Field(default=None, ge=0)
    error: str | None = None
    reason: str | None = None


class CatalogDependencyCheck(DependencyCheck):
    loaded: bool
    current_revision_id: str | None
    current_revision: int | None = Field(default=None, ge=1)


class OutboxStatusSnapshot(_DiagnosticsModel):
    pending_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    oldest_pending_at: str | None


class OutboxDependencyCheck(DependencyCheck):
    snapshot: OutboxStatusSnapshot | None
    limit: int = Field(ge=1, le=1_000)


class BursarRuntimeDiagnostics(_DiagnosticsModel):
    checked_at: str
    ready: bool
    financial_ready: bool
    projection_ready: bool
    degraded: bool
    state: BursarRuntimeState
    postgres: DependencyCheck
    catalog: CatalogDependencyCheck
    outbox: OutboxDependencyCheck


class CheckDependenciesOptions(_DiagnosticsModel):
    outbox_limit: int = Field(default=100, strict=True, ge=1, le=1_000)


class CatalogRevisionSnapshot(_DiagnosticsModel):
    id: str = Field(min_length=1)
    version: int = Field(strict=True, ge=1)


class RuntimeStateInput(_DiagnosticsModel):
    started: bool
    closed: bool
    catalog_loaded: bool


class RuntimeDiagnosticsOperations(_DiagnosticsModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    check_postgres: Callable[[], None]
    get_catalog_revision: Callable[[], CatalogRevisionSnapshot | None]
    get_outbox_status: Callable[[int], OutboxStatusSnapshot] | None = None


class RuntimeDiagnosticsTracker:
    """Thread-safe local worker state plus active dependency checks."""

    def __init__(self, operations: RuntimeDiagnosticsOperations, *, worker_configured: bool) -> None:
        self._operations = operations
        self._worker_configured = worker_configured
        self._worker_started = False
        self._worker_stopped = False
        self._last_run: WorkerRunSnapshot | None = None
        self._last_error: WorkerErrorSnapshot | None = None
        self._lock = threading.RLock()

    def mark_worker_started(self) -> None:
        with self._lock:
            if self._worker_configured:
                self._worker_started = True

    def mark_worker_stopped(self) -> None:
        with self._lock:
            if self._worker_configured:
                self._worker_stopped = True

    def record_worker_error(self, error: BaseException) -> None:
        with self._lock:
            self._last_error = WorkerErrorSnapshot(
                at=datetime.now(UTC).isoformat(),
                error=persisted_diagnostic_summary(error, "outbox_worker_failed"),
            )

    def observe_manual_run(self, operation: Callable[[], OutboxRunResult]) -> OutboxRunResult:
        started_at = datetime.now(UTC).isoformat()
        try:
            result = operation()
        except Exception as error:
            completed_at = datetime.now(UTC).isoformat()
            message = persisted_diagnostic_summary(error, "outbox_worker_failed")
            with self._lock:
                self._last_run = WorkerRunSnapshot(
                    status="failed",
                    started_at=started_at,
                    completed_at=completed_at,
                    result=None,
                    error=message,
                )
                self._last_error = WorkerErrorSnapshot(at=completed_at, error=message)
            raise
        with self._lock:
            self._last_run = WorkerRunSnapshot(
                status="completed",
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                result=result,
            )
        return result

    def state(self, input: RuntimeStateInput) -> BursarRuntimeState:
        with self._lock:
            financial_ready = input.started and not input.closed and input.catalog_loaded
            projection_ready = not self._worker_configured or self._worker_lifecycle(input.closed) == "running"
            return BursarRuntimeState(
                ready=financial_ready and projection_ready,
                financial_ready=financial_ready,
                projection_ready=projection_ready,
                degraded=financial_ready and not projection_ready,
                started=input.started,
                closed=input.closed,
                catalog_loaded=input.catalog_loaded,
                worker=WorkerState(
                    configured=self._worker_configured,
                    lifecycle=self._worker_lifecycle(input.closed),
                    last_run=self._last_run,
                    last_error=self._last_error,
                ),
            )

    def check_dependencies(
        self,
        input: RuntimeStateInput,
        options: CheckDependenciesOptions | None = None,
    ) -> BursarRuntimeDiagnostics:
        effective = options or CheckDependenciesOptions()
        state = self.state(input)
        if input.closed:
            skipped = DependencyCheck(status="skipped", reason="runtime is closed")
            return BursarRuntimeDiagnostics(
                checked_at=datetime.now(UTC).isoformat(),
                ready=False,
                financial_ready=False,
                projection_ready=False,
                degraded=False,
                state=state,
                postgres=skipped,
                catalog=CatalogDependencyCheck(
                    **skipped.model_dump(),
                    loaded=input.catalog_loaded,
                    current_revision_id=None,
                    current_revision=None,
                ),
                outbox=OutboxDependencyCheck(
                    **skipped.model_dump(),
                    snapshot=None,
                    limit=effective.outbox_limit,
                ),
            )

        postgres = _check_dependency(self._operations.check_postgres, "postgres_check_failed")
        catalog = self._check_catalog(input.catalog_loaded)
        outbox = self._check_outbox(effective.outbox_limit)
        financial_ready = state.financial_ready and postgres.status == "ok" and catalog.status == "ok"
        projection_ready = state.projection_ready and (
            not self._worker_configured
            or (outbox.status == "ok" and outbox.snapshot is not None and outbox.snapshot.dead_letter_count == 0)
        )
        return BursarRuntimeDiagnostics(
            checked_at=datetime.now(UTC).isoformat(),
            ready=financial_ready and projection_ready,
            financial_ready=financial_ready,
            projection_ready=projection_ready,
            degraded=financial_ready and not projection_ready,
            state=state,
            postgres=postgres,
            catalog=catalog,
            outbox=outbox,
        )

    def _worker_lifecycle(
        self,
        closed: bool,
    ) -> WorkerLifecycle:
        if not self._worker_configured:
            return "not_configured"
        if closed or self._worker_stopped:
            return "stopped"
        return "running" if self._worker_started else "not_started"

    def _check_catalog(self, loaded: bool) -> CatalogDependencyCheck:
        started = perf_counter()
        try:
            revision = self._operations.get_catalog_revision()
            return CatalogDependencyCheck(
                status="ok",
                latency_ms=_elapsed_ms(started),
                loaded=loaded,
                current_revision_id=revision.id if revision is not None else None,
                current_revision=revision.version if revision is not None else None,
            )
        except Exception as error:
            return CatalogDependencyCheck(
                status="error",
                latency_ms=_elapsed_ms(started),
                loaded=loaded,
                current_revision_id=None,
                current_revision=None,
                error=persisted_diagnostic_summary(error, "catalog_check_failed"),
            )

    def _check_outbox(self, limit: int) -> OutboxDependencyCheck:
        if self._operations.get_outbox_status is None:
            return OutboxDependencyCheck(
                status="unsupported",
                reason="the configured outbox store does not expose bounded status",
                snapshot=None,
                limit=limit,
            )
        started = perf_counter()
        try:
            snapshot = self._operations.get_outbox_status(limit)
            return OutboxDependencyCheck(
                status="ok",
                latency_ms=_elapsed_ms(started),
                snapshot=snapshot,
                limit=limit,
            )
        except Exception as error:
            return OutboxDependencyCheck(
                status="error",
                latency_ms=_elapsed_ms(started),
                snapshot=None,
                limit=limit,
                error=persisted_diagnostic_summary(error, "outbox_check_failed"),
            )


def _check_dependency(operation: Callable[[], None], fallback: str) -> DependencyCheck:
    started = perf_counter()
    try:
        operation()
        return DependencyCheck(status="ok", latency_ms=_elapsed_ms(started))
    except Exception as error:
        return DependencyCheck(
            status="error",
            latency_ms=_elapsed_ms(started),
            error=persisted_diagnostic_summary(error, fallback),
        )


def _elapsed_ms(started: float) -> float:
    return max((perf_counter() - started) * 1_000, 0.0)
