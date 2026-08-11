"""Node-style composition root for Python storage infrastructure."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from inspect import Parameter, signature
from typing import Literal, Protocol, TypeGuard
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, field_validator, model_validator

from bursar.billing.postgres.store import PostgresBillingStore
from bursar.billing.service_types import BillingServiceOptions
from bursar.bursar import Bursar
from bursar.commerce.types import CommerceOptions, CommerceRuntimeOptions
from bursar.credits.events import CreditEventEmitter
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service_types import CreditsServiceOptions
from bursar.credits.types import UsageAnalyticsStore
from bursar.errors import CatalogNotLoadedError, ConfigError, StoreClosedError, is_retryable_bursar_error
from bursar.providers.types import ProviderEnvironment
from bursar.retry import BursarRetryOptions, retry_bursar_operation
from bursar.shared.postgres_client import PostgresClient, PostgresConnectionOptions, PostgresPool, create_pool
from bursar.storage.adapters.clickhouse import (
    ClickHouseUsageStore,
    ClickHouseUsageStoreOptions,
)
from bursar.storage.adapters.s3 import S3BillingArchive, S3BillingArchiveOptions
from bursar.storage.diagnostics import (
    BursarRuntimeDiagnostics,
    BursarRuntimeState,
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
)
from bursar.storage.outbox_worker import (
    OutboxRunResult,
    OutboxWorker,
    OutboxWorkerOptions,
)
from bursar.storage.ports import (
    BillingEventPayloadExport,
    BillingPayloadArchive,
    OutboxEvent,
    OutboxRecoveryStore,
    UsageChargeExport,
    UsageEventSink,
)
from bursar.storage.postgres_repository import PostgresStorageRepository


class UsageAnalyticsSink(UsageEventSink, UsageAnalyticsStore, Protocol):
    """Combined ClickHouse write and analytics read port."""


def _is_usage_analytics_sink(value: object) -> TypeGuard[UsageAnalyticsSink]:
    return all(
        hasattr(value, method)
        for method in (
            "write_usage",
            "spend_by_user",
            "spend_by_model",
            "top_users",
            "daily_spend",
            "aggregate_stats",
        )
    )


def _is_billing_payload_archive(value: object) -> TypeGuard[BillingPayloadArchive]:
    return hasattr(value, "archive")


def _is_usage_charge_store(value: object) -> bool:
    return hasattr(value, "list_usage_charges")


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class BursarRuntimeBursarOptions(_RuntimeModel):
    credits_options: CreditsServiceOptions | None = None
    billing_options: BillingServiceOptions | None = None
    commerce_options: CommerceRuntimeOptions | None = None
    emitter: SkipValidation[CreditEventEmitter] | None = None


class BursarRuntimeOptions(_RuntimeModel):
    postgres: str | SkipValidation[PostgresPool]
    operator_postgres: str | SkipValidation[PostgresPool]
    tenant_id: UUID
    provider_environment: ProviderEnvironment
    tenant_slug: str | None = None
    postgres_options: SkipValidation[PostgresConnectionOptions] = Field(default_factory=PostgresConnectionOptions)
    s3: SkipValidation[BillingPayloadArchive] | S3BillingArchiveOptions | None = None
    clickhouse: SkipValidation[UsageAnalyticsSink] | ClickHouseUsageStoreOptions | None = None
    outbox: OutboxWorkerOptions | Literal[False] | None = None
    bursar: BursarRuntimeBursarOptions = Field(default_factory=BursarRuntimeBursarOptions)

    @field_validator("tenant_slug")
    @classmethod
    def validate_tenant_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not 1 <= len(normalized) <= 100 or re.fullmatch(r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?", normalized) is None:
            raise ValueError("tenant_slug must be a valid Bursar tenant slug")
        return normalized

    @model_validator(mode="after")
    def validate_outbox(self) -> BursarRuntimeOptions:
        if isinstance(self.outbox, bool) and self.outbox is not False:
            msg = "outbox must be OutboxWorkerOptions, False, or None"
            raise ValueError(msg)
        credits_options = self.bursar.credits_options
        if credits_options is not None and (
            credits_options.analytics is not None or credits_options.usage_store is not None
        ):
            raise ValueError("configure usage analytics through BursarRuntimeOptions.clickhouse")
        return self


class BursarRuntimeStartOptions(_RuntimeModel):
    load_catalog: bool = Field(default=True, strict=True)
    max_attempts: int = Field(default=1, strict=True, ge=1)
    retry_delay_seconds: float = Field(default=0.25, strict=True, ge=0, le=5.0, allow_inf_nan=False)
    max_elapsed_seconds: float = Field(default=30.0, strict=True, ge=0, allow_inf_nan=False)
    should_retry: Callable[[BaseException], bool] | None = None


class BursarRuntimeHealth(_RuntimeModel):
    ready: bool
    financial_ready: bool
    projection_ready: bool
    degraded: bool
    started: bool
    closed: bool
    catalog_loaded: bool


@dataclass(frozen=True, slots=True)
class _CallableOutboxHandler:
    topics: Sequence[str]
    callback: Callable[[OutboxEvent], None]

    def handle(self, event: OutboxEvent) -> None:
        self.callback(event)


@dataclass(slots=True)
class _PendingUsageWrite:
    event: UsageChargeExport
    outbox_event_id: str
    completed: threading.Event
    error: BaseException | None = None


class _UsageWriteBatcher:
    """Coalesce concurrently dispatched usage events into one optional sink write."""

    def __init__(self, sink: UsageAnalyticsSink) -> None:
        self._sink = sink
        write_batch = getattr(sink, "write_usage_batch", None)
        self._write_batch: Callable[[Sequence[tuple[UsageChargeExport, str]]], object] | None = (
            write_batch if callable(write_batch) else None
        )
        self._lock = threading.Lock()
        self._pending: list[_PendingUsageWrite] = []
        self._timer: threading.Timer | None = None

    def write(self, event: UsageChargeExport, outbox_event_id: str) -> None:
        if not callable(self._write_batch):
            self._sink.write_usage(event, outbox_event_id)
            return

        pending = _PendingUsageWrite(event, outbox_event_id, threading.Event())
        with self._lock:
            self._pending.append(pending)
            if self._timer is None:
                self._timer = threading.Timer(0.001, self._flush)
                self._timer.daemon = True
                self._timer.start()
        pending.completed.wait()
        if pending.error is not None:
            raise pending.error

    def _flush(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = []
            self._timer = None
        write_batch = self._write_batch
        try:
            if write_batch is None:  # pragma: no cover - only scheduled when batching is supported
                raise RuntimeError("usage batch writer is unavailable")
            write_batch([(item.event, item.outbox_event_id) for item in pending])
        except BaseException as error:
            for item in pending:
                item.error = error
        finally:
            for item in pending:
                item.completed.set()


class BursarRuntime:
    """Composition root for Postgres and optional external projections."""

    bursar: Bursar
    credit_store: PostgresStore
    billing_store: PostgresBillingStore
    maintenance: BursarMaintenance
    operator_maintenance: BursarOperatorMaintenance
    worker: OutboxWorker | None
    outbox_recovery: OutboxRecoveryStore
    clickhouse: UsageAnalyticsSink | None
    s3: BillingPayloadArchive | None

    def __init__(self, options: BursarRuntimeOptions) -> None:
        """Construct a runtime while keeping pool ownership inside the SDK."""
        if isinstance(options.postgres, str) and not options.postgres.strip():
            msg = "postgres connection string must not be empty"
            raise ValueError(msg)
        if isinstance(options.operator_postgres, str) and not options.operator_postgres.strip():
            msg = "operator_postgres connection string must not be empty"
            raise ValueError(msg)
        if options.postgres is options.operator_postgres or (
            isinstance(options.postgres, str)
            and isinstance(options.operator_postgres, str)
            and options.postgres == options.operator_postgres
        ):
            msg = "postgres and operator_postgres must use distinct connections"
            raise ValueError(msg)

        pool: PostgresPool | None = None
        operator_pool: PostgresPool | None = None
        owns_pool = False
        owns_operator_pool = False
        try:
            if isinstance(options.postgres, str):
                pool = create_pool(
                    options.postgres,
                    postgres_options=options.postgres_options,
                )
                owns_pool = True
            else:
                pool = options.postgres

            if isinstance(options.operator_postgres, str):
                operator_pool = create_pool(
                    options.operator_postgres,
                    postgres_options=options.postgres_options,
                )
                owns_operator_pool = True
            else:
                operator_pool = options.operator_postgres

            self._initialize(
                pool,
                owns_pool,
                operator_pool,
                owns_operator_pool,
                options,
            )
        except BaseException:
            # Release any partially-composed adapters without closing a pool
            # supplied by the caller.
            for name in ("s3", "credit_store", "billing_store", "_postgres"):
                resource = getattr(self, name, None)
                close = getattr(resource, "close", None)
                if callable(close):
                    with suppress(BaseException):
                        close()
            if owns_pool and pool is not None:
                with suppress(BaseException):
                    pool.closeall()
            if owns_operator_pool and operator_pool is not None:
                with suppress(BaseException):
                    operator_pool.closeall()
            raise

    def _initialize(
        self,
        pool: PostgresPool,
        owns_pool: bool,
        operator_pool: PostgresPool,
        owns_operator_pool: bool,
        options: BursarRuntimeOptions,
    ) -> None:
        self._pool = pool
        self._operator_pool = operator_pool
        self._owns_pool = owns_pool
        self._owns_operator_pool = owns_operator_pool
        self.clickhouse: UsageAnalyticsSink | None
        if options.clickhouse is None:
            self.clickhouse = None
        elif isinstance(options.clickhouse, ClickHouseUsageStoreOptions):
            if options.clickhouse.tenant_id != options.tenant_id:
                msg = "ClickHouse tenant_id must match runtime tenant_id"
                raise ValueError(msg)
            self.clickhouse = ClickHouseUsageStore(options.clickhouse)
        elif _is_usage_analytics_sink(options.clickhouse):
            self.clickhouse = options.clickhouse
        else:
            msg = "clickhouse must implement both usage sink and analytics ports"
            raise TypeError(msg)
        self._usage_batcher = _UsageWriteBatcher(self.clickhouse) if self.clickhouse is not None else None
        self.s3: BillingPayloadArchive | None
        if options.s3 is None:
            self.s3 = None
        elif isinstance(options.s3, S3BillingArchiveOptions):
            self.s3 = S3BillingArchive(options.s3)
        elif _is_billing_payload_archive(options.s3):
            self.s3 = options.s3
        else:
            msg = "s3 must implement the billing payload archive port"
            raise TypeError(msg)

        self.credit_store = PostgresStore(
            tenant_id=options.tenant_id,
            pool=pool,
            usage_backend="clickhouse" if self.clickhouse is not None else "postgres",
            provider_environment=options.provider_environment,
            postgres_options=options.postgres_options,
        )
        self.billing_store = PostgresBillingStore(
            tenant_id=options.tenant_id,
            provider_environment=options.provider_environment,
            pool=pool,
            billing_payload_backend="s3" if self.s3 is not None else "postgres",
            postgres_options=options.postgres_options,
        )
        credits_options = (options.bursar.credits_options or CreditsServiceOptions()).model_copy(
            update={
                "analytics": self.clickhouse,
                "usage_store": (
                    self.clickhouse if self.clickhouse is not None and _is_usage_charge_store(self.clickhouse) else None
                ),
            }
        )
        runtime_commerce_options = options.bursar.commerce_options
        commerce_options = (
            CommerceOptions.model_validate(
                {
                    **runtime_commerce_options.model_dump(),
                    "tenant_id": str(options.tenant_id),
                    "provider_environment": options.provider_environment,
                }
            )
            if runtime_commerce_options is not None
            else None
        )
        self.bursar = Bursar(
            credit_store=self.credit_store,
            billing_store=self.billing_store,
            credits_options=credits_options,
            billing_options=options.bursar.billing_options,
            commerce_options=commerce_options,
            emitter=options.bursar.emitter,
        )

        self._postgres = PostgresClient.from_pool(
            operator_pool,
            tenant_id=options.tenant_id,
            access_role="bursar_operator",
            provider_environment=options.provider_environment,
            usage_backend="clickhouse" if self.clickhouse is not None else "postgres",
            billing_payload_backend="s3" if self.s3 is not None else "postgres",
            postgres_options=options.postgres_options,
        )
        self._query = self._postgres.query
        self._tenant_id = str(options.tenant_id)
        self._tenant_slug = options.tenant_slug
        self.maintenance = BursarMaintenance(
            MaintenanceOperations(
                expire_leases=self.credit_store.expire_leases,
                expire_credits=lambda limit: (
                    self.credit_store.sweep_expired_credits(
                        dry_run=False,
                        user_id=None,
                        limit=limit,
                    ).expired_count
                ),
                apply_due_plan_changes=self.credit_store.apply_due_plan_changes,
                expire_past_due_grace_periods=(
                    self.bursar.billing.expire_past_due_grace_periods if self.bursar.billing is not None else None
                ),
                past_due_grace_period_limit=100,
                past_due_grace_periods_unavailable_reason=(
                    None if self.bursar.billing is not None else "billing is not configured"
                ),
            )
        )
        self.operator_maintenance = BursarOperatorMaintenance(self._query)
        repository = PostgresStorageRepository(
            self._query,
            options.tenant_id,
        )
        self.outbox_recovery = repository
        handlers = self._create_handlers(repository)
        worker_options = options.outbox if isinstance(options.outbox, OutboxWorkerOptions) else None
        original_on_error = worker_options.on_error if worker_options is not None else None

        def on_worker_error(error: BaseException) -> None:
            diagnostics = getattr(self, "_diagnostics", None)
            if diagnostics is not None:
                diagnostics.record_worker_error(error)
            if original_on_error is not None:
                original_on_error(error)

        effective_worker_options = (worker_options or OutboxWorkerOptions()).model_copy(
            update={"on_error": on_worker_error}
        )
        self.worker = (
            OutboxWorker(repository, handlers, effective_worker_options)
            if handlers and options.outbox is not False
            else None
        )
        self._diagnostics = RuntimeDiagnosticsTracker(
            RuntimeDiagnosticsOperations(
                check_postgres=self._check_postgres,
                get_catalog_revision=self._get_catalog_revision,
                get_outbox_status=_outbox_status_provider(repository),
            ),
            worker_configured=self.worker is not None,
        )
        self._lifecycle_lock = threading.RLock()
        self._started = False
        self._closed = False
        self._close_failure: BaseException | None = None

    def start(self, options: BursarRuntimeStartOptions | None = None) -> None:
        with self._lifecycle_lock:
            if self._closed:
                msg = "BursarRuntime has been closed"
                raise StoreClosedError(msg)
            if self._started:
                return
            start_options = options or BursarRuntimeStartOptions()
            self._verify_tenant_identity()
            if start_options.load_catalog:
                retry_bursar_operation(
                    self.bursar.load_catalog,
                    retry_options=BursarRetryOptions(
                        max_attempts=start_options.max_attempts,
                        base_delay_seconds=start_options.retry_delay_seconds,
                        max_delay_seconds=5.0,
                        max_elapsed_seconds=start_options.max_elapsed_seconds,
                        should_retry=start_options.should_retry
                        or (lambda error: isinstance(error, CatalogNotLoadedError) or is_retryable_bursar_error(error)),
                    ),
                )
            if self.clickhouse is not None:
                initialize = getattr(self.clickhouse, "initialize", None)
                if callable(initialize):
                    initialize()
                check_schema_compatibility = getattr(self.clickhouse, "check_schema_compatibility", None)
                if callable(check_schema_compatibility):
                    check_schema_compatibility()
            if self.worker is not None:
                self.worker.start()
                self._diagnostics.mark_worker_started()
            self._started = True

    def state(self) -> BursarRuntimeState:
        with self._lifecycle_lock:
            return self._diagnostics.state(
                RuntimeStateInput(
                    started=self._started,
                    closed=self._closed,
                    catalog_loaded=self.bursar.catalog.is_loaded,
                )
            )

    def check_dependencies(
        self,
        options: CheckDependenciesOptions | None = None,
    ) -> BursarRuntimeDiagnostics:
        with self._lifecycle_lock:
            state_input = RuntimeStateInput(
                started=self._started,
                closed=self._closed,
                catalog_loaded=self.bursar.catalog.is_loaded,
            )
        return self._diagnostics.check_dependencies(state_input, options)

    def health(self) -> BursarRuntimeHealth:
        state = self.state()
        return BursarRuntimeHealth(
            ready=state.ready,
            financial_ready=state.financial_ready,
            projection_ready=state.projection_ready,
            degraded=state.degraded,
            started=state.started,
            closed=state.closed,
            catalog_loaded=state.catalog_loaded,
        )

    def flush(self) -> OutboxRunResult:
        with self._lifecycle_lock:
            if self._closed:
                msg = "BursarRuntime has been closed"
                raise StoreClosedError(msg)
            if self.worker is None:
                return OutboxRunResult(claimed=0, delivered=0, failed=0, claim_lost=0)
            return self._diagnostics.observe_manual_run(self.worker.run_once)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._close_failure is not None:
                raise self._close_failure
            if self._closed:
                return
            self._closed = True
            failures: list[BaseException] = []

            resources: list[Callable[[], object]] = []
            if self.worker is not None:
                resources.append(self._stop_worker)
            if self.s3 is not None:
                close = getattr(self.s3, "close", None)
                if callable(close):
                    resources.append(close)
            resources.extend((self.credit_store.close, self.billing_store.close, self._postgres.close))
            if self._owns_pool:
                resources.append(self._pool.closeall)
            if self._owns_operator_pool:
                resources.append(self._operator_pool.closeall)

            for close_resource in resources:
                try:
                    close_resource()
                except BaseException as error:
                    failures.append(error)

            if not failures:
                return
            self._close_failure = (
                failures[0]
                if len(failures) == 1
                else BaseExceptionGroup("BursarRuntime failed to close all resources", failures)
            )
            raise self._close_failure

    def _stop_worker(self) -> None:
        try:
            if self.worker is not None:
                self.worker.stop()
        finally:
            self._diagnostics.mark_worker_stopped()

    def __enter__(self) -> BursarRuntime:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _verify_tenant_identity(self) -> None:
        if self._tenant_slug is None:
            return
        rows = self._query(
            "SELECT bursar.resolve_active_tenant_for_trigger(%s)::text AS tenant_id",
            [self._tenant_slug],
        )
        resolved = rows[0].get("tenant_id") if rows else None
        if resolved != self._tenant_id:
            raise ConfigError(f"Bursar tenant slug '{self._tenant_slug}' resolves to a different tenant ID")

    def _check_postgres(self) -> None:
        rows = self._query("SELECT 1 AS bursar_reachable")
        if len(rows) != 1 or rows[0].get("bursar_reachable") != 1:
            raise RuntimeError("PostgreSQL reachability check returned an invalid result")

    def _get_catalog_revision(self) -> CatalogRevisionSnapshot | None:
        revision = self.bursar.catalog.get_active()
        if revision is None:
            return None
        return CatalogRevisionSnapshot(id=revision.id, version=revision.version)

    def _create_handlers(
        self,
        repository: PostgresStorageRepository,
    ) -> list[_CallableOutboxHandler]:
        handlers: list[_CallableOutboxHandler] = []
        if self.clickhouse is not None:
            handlers.append(
                _CallableOutboxHandler(
                    topics=("usage.charge_recorded",),
                    callback=partial(self._handle_usage, repository),
                )
            )
        if self.s3 is not None:
            handlers.append(
                _CallableOutboxHandler(
                    topics=("billing.webhook_received", "billing.webhook_completed"),
                    callback=partial(self._handle_billing, repository),
                )
            )
        return handlers

    def _handle_usage(self, repository: PostgresStorageRepository, outbox_event: OutboxEvent) -> None:
        if outbox_event.payload_version != 1:
            msg = f"Unsupported usage outbox payload version {outbox_event.payload_version}"
            raise RuntimeError(msg)
        usage = None
        if outbox_event.payload.get("charge_id") is not None:
            try:
                usage = UsageChargeExport.model_validate(
                    {key: value for key, value in outbox_event.payload.items() if key != "delivery_required"}
                )
            except ValueError:
                usage = None
        if usage is None:
            usage = repository.get_usage_charge(outbox_event.aggregate_id)
        if usage is None:
            msg = f"Usage charge {outbox_event.aggregate_id} is unavailable for export"
            raise RuntimeError(msg)
        if usage.tenant_id != outbox_event.tenant_id:
            raise RuntimeError("Usage export tenant does not match its outbox event")
        if usage.charge_id != outbox_event.aggregate_id:
            raise RuntimeError("Usage export charge does not match its outbox event")
        if self._usage_batcher is None:
            raise RuntimeError("ClickHouse usage sink is not configured")
        self._usage_batcher.write(usage, outbox_event.event_id)

    def _handle_billing(self, repository: PostgresStorageRepository, outbox_event: OutboxEvent) -> None:
        if outbox_event.payload_version != 1:
            msg = f"Unsupported billing outbox payload version {outbox_event.payload_version}"
            raise RuntimeError(msg)
        stored = repository.get_billing_event_payload(outbox_event.aggregate_id)
        if stored is not None and stored.object_key is not None:
            return
        if outbox_event.topic == "billing.webhook_received":
            event = BillingEventPayloadExport.model_validate(
                {key: value for key, value in outbox_event.payload.items() if key != "delivery_required"}
            )
        else:
            event = stored
        if event is None:
            msg = f"Billing event {outbox_event.aggregate_id} is unavailable for archive"
            raise RuntimeError(msg)
        if event.tenant_id != outbox_event.tenant_id:
            raise RuntimeError("Billing export tenant does not match its outbox event")
        if event.event_id != outbox_event.aggregate_id:
            raise RuntimeError("Billing export event does not match its outbox event")
        if self.s3 is None:
            raise RuntimeError("S3 archive is not configured")
        archived = self.s3.archive(event)
        recorded = repository.archive_billing_event_payload(
            event.event_id,
            archived.key,
            archived.version_id,
        )
        if not recorded:
            msg = f"Could not record archive pointer for billing event {event.event_id}"
            raise RuntimeError(msg)


def _outbox_status_provider(
    repository: PostgresStorageRepository,
) -> Callable[[int], OutboxStatusSnapshot] | None:
    stats = getattr(repository, "stats", None)
    if not callable(stats):
        return None

    def get_status(limit: int) -> OutboxStatusSnapshot:
        raw = _invoke_outbox_stats(stats, limit)
        values = _stats_mapping(raw)
        return OutboxStatusSnapshot(
            pending_count=_stats_count(values, "pending_count", "pendingCount", "pending"),
            processing_count=_stats_count(values, "processing_count", "processingCount", "processing"),
            delivered_count=_stats_count(values, "delivered_count", "deliveredCount", "delivered"),
            dead_letter_count=_stats_count(values, "dead_letter_count", "deadLetterCount", "dead_letter"),
            oldest_pending_at=_stats_timestamp(values, "oldest_pending_at", "oldestPendingAt"),
        )

    return get_status


def _invoke_outbox_stats(stats: Callable[..., object], limit: int) -> object:
    try:
        parameters = list(signature(stats).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    if not parameters:
        return stats()
    if any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters):
        return stats(outbox_limit=limit)
    first = parameters[0]
    if first.kind == Parameter.POSITIONAL_ONLY:
        return stats(limit)
    if first.name in {"limit", "outbox_limit"}:
        return stats(**{first.name: limit})
    return stats({"limit": limit, "outbox_limit": limit})


def _stats_mapping(raw: object) -> Mapping[str, object]:
    if isinstance(raw, BaseModel):
        return raw.model_dump()
    if isinstance(raw, Mapping):
        return raw
    values = {
        name: getattr(raw, name)
        for name in (
            "pending_count",
            "pendingCount",
            "pending",
            "processing_count",
            "processingCount",
            "processing",
            "delivered_count",
            "deliveredCount",
            "delivered",
            "dead_letter_count",
            "deadLetterCount",
            "dead_letter",
            "oldest_pending_at",
            "oldestPendingAt",
        )
        if hasattr(raw, name)
    }
    if not values:
        raise TypeError("outbox stats returned a malformed result")
    return values


def _stats_count(values: Mapping[str, object], *keys: str) -> int:
    value = next((values[key] for key in keys if key in values), None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"outbox stats field {keys[0]} must be a non-negative integer")
    return value


def _stats_timestamp(values: Mapping[str, object], *keys: str) -> str | None:
    value = next((values[key] for key in keys if key in values), None)
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(f"outbox stats field {keys[0]} must include a timezone")
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError(f"outbox stats field {keys[0]} must be a timestamp string or null")


def create_bursar_runtime(options: BursarRuntimeOptions) -> BursarRuntime:
    return BursarRuntime(options)
