"""Node-style composition root for Python storage infrastructure."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol, TypeGuard, cast
from uuid import UUID

import psycopg2.pool
from pydantic import BaseModel, ConfigDict, SkipValidation, model_validator

from bursar.billing.postgres.store import PostgresBillingStore
from bursar.billing.service_types import BillingServiceOptions
from bursar.bursar import Bursar
from bursar.commerce.types import CommerceOptions
from bursar.credits.events import CreditEventEmitter
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service_types import CreditsServiceOptions
from bursar.credits.types import UsageAnalyticsStore
from bursar.errors import PricingNotLoadedError, is_retryable_bursar_error
from bursar.shared.postgres_client import PostgresClient
from bursar.storage.adapters.clickhouse import (
    ClickHouseUsageStore,
    ClickHouseUsageStoreOptions,
)
from bursar.storage.adapters.s3 import S3BillingArchive, S3BillingArchiveOptions
from bursar.storage.outbox_worker import (
    OutboxRunResult,
    OutboxWorker,
    OutboxWorkerOptions,
)
from bursar.storage.ports import (
    BillingEventPayloadExport,
    BillingPayloadArchive,
    OutboxEvent,
    UsageChargeExport,
    UsageEventSink,
)
from bursar.storage.postgres_repository import PostgresStorageRepository


class UsageAnalyticsSink(UsageEventSink, UsageAnalyticsStore, Protocol):
    """Combined ClickHouse write and analytics read port."""


class PostgresPool(Protocol):
    def getconn(self) -> Any: ...

    def putconn(self, conn: Any) -> None: ...

    def closeall(self) -> None: ...


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
    return hasattr(value, "archive") and hasattr(value, "purge_postgres_payload")


def _is_usage_charge_store(value: object) -> bool:
    return hasattr(value, "list_usage_charges")


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class BursarRuntimeBursarOptions(_RuntimeModel):
    credits_options: CreditsServiceOptions | None = None
    billing_options: BillingServiceOptions | None = None
    commerce_options: CommerceOptions | None = None
    emitter: SkipValidation[CreditEventEmitter] | None = None


class BursarRuntimeOptions(_RuntimeModel):
    postgres: str | SkipValidation[PostgresPool]
    tenant_id: UUID
    s3: SkipValidation[BillingPayloadArchive] | S3BillingArchiveOptions | None = None
    clickhouse: SkipValidation[UsageAnalyticsSink] | ClickHouseUsageStoreOptions | None = None
    outbox: OutboxWorkerOptions | Literal[False] | None = None
    bursar: BursarRuntimeBursarOptions = BursarRuntimeBursarOptions()

    @model_validator(mode="after")
    def validate_outbox(self) -> BursarRuntimeOptions:
        if isinstance(self.outbox, bool) and self.outbox is not False:
            msg = "outbox must be OutboxWorkerOptions, False, or None"
            raise ValueError(msg)
        return self


class BursarRuntimeStartOptions(_RuntimeModel):
    load_catalog: bool = False
    max_attempts: int = 1
    retry_delay_seconds: float = 0.25
    should_retry: SkipValidation[Callable[[BaseException], bool]] | None = None

    @model_validator(mode="after")
    def validate_retry(self) -> BursarRuntimeStartOptions:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        return self


class BursarRuntimeHealth(_RuntimeModel):
    ready: bool
    started: bool
    closed: bool
    catalog_loaded: bool


@dataclass(frozen=True, slots=True)
class _CallableOutboxHandler:
    topics: Sequence[str]
    callback: Callable[[OutboxEvent], None]

    def handle(self, event: OutboxEvent) -> None:
        self.callback(event)


class BursarRuntime:
    """Composition root for Postgres and optional external projections."""

    bursar: Bursar
    credit_store: PostgresStore
    billing_store: PostgresBillingStore
    worker: OutboxWorker | None
    clickhouse: UsageAnalyticsSink | None
    s3: BillingPayloadArchive | None

    def __init__(
        self,
        pool: PostgresPool,
        owns_pool: bool,
        options: BursarRuntimeOptions,
    ) -> None:
        self._pool = pool
        self._owns_pool = owns_pool
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

        psycopg_pool = cast(psycopg2.pool.ThreadedConnectionPool, pool)
        self.credit_store = PostgresStore(
            "",
            tenant_id=options.tenant_id,
            pool=psycopg_pool,
            usage_backend="clickhouse" if self.clickhouse is not None else "postgres",
        )
        self.billing_store = PostgresBillingStore(
            "",
            tenant_id=options.tenant_id,
            pool=psycopg_pool,
            billing_payload_backend="s3" if self.s3 is not None else "postgres",
        )
        credits_options = (options.bursar.credits_options or CreditsServiceOptions()).model_copy(
            update={
                "analytics": self.clickhouse,
                "usage_store": (
                    self.clickhouse if self.clickhouse is not None and _is_usage_charge_store(self.clickhouse) else None
                ),
            }
        )
        commerce_options = options.bursar.commerce_options
        if commerce_options is not None:
            commerce_options = commerce_options.model_copy(update={"tenant_id": str(options.tenant_id)})
        self.bursar = Bursar.create(
            credit_store=self.credit_store,
            billing_store=self.billing_store,
            credits_options=credits_options,
            billing_options=options.bursar.billing_options,
            commerce_options=commerce_options,
            emitter=options.bursar.emitter,
        )

        repository = PostgresStorageRepository(
            PostgresClient.from_pool(
                psycopg_pool,
                tenant_id=options.tenant_id,
                usage_backend="clickhouse" if self.clickhouse is not None else "postgres",
                billing_payload_backend="s3" if self.s3 is not None else "postgres",
            ).query,
            options.tenant_id,
        )
        handlers = self._create_handlers(repository)
        worker_options = options.outbox if isinstance(options.outbox, OutboxWorkerOptions) else None
        self.worker = (
            OutboxWorker(repository, handlers, worker_options) if handlers and options.outbox is not False else None
        )
        self._started = False
        self._closed = False

    def start(self, options: BursarRuntimeStartOptions | None = None) -> None:
        if self._started:
            return
        if self._closed:
            msg = "BursarRuntime has been closed"
            raise RuntimeError(msg)
        start_options = options or BursarRuntimeStartOptions()
        if start_options.load_catalog:
            for attempt in range(1, start_options.max_attempts + 1):
                try:
                    self.bursar.load_catalog()
                    break
                except Exception as exc:
                    retryable = (
                        start_options.should_retry(exc)
                        if start_options.should_retry is not None
                        else isinstance(exc, PricingNotLoadedError) or is_retryable_bursar_error(exc)
                    )
                    if attempt >= start_options.max_attempts or not retryable:
                        raise
                    delay = min(start_options.retry_delay_seconds * (2 ** (attempt - 1)), 5.0)
                    time.sleep(delay)
        if self.clickhouse is not None:
            initialize = getattr(self.clickhouse, "initialize", None)
            if callable(initialize):
                initialize()
        if self.worker is not None:
            self.worker.start()
        self._started = True

    def health(self) -> BursarRuntimeHealth:
        catalog_loaded = self.bursar.credits.pricing_engine is not None
        return BursarRuntimeHealth(
            ready=self._started and not self._closed and catalog_loaded,
            started=self._started,
            closed=self._closed,
            catalog_loaded=catalog_loaded,
        )

    def flush(self) -> OutboxRunResult:
        if self._closed:
            msg = "BursarRuntime has been closed"
            raise RuntimeError(msg)
        if self.worker is None:
            return OutboxRunResult(claimed=0, delivered=0, failed=0)
        return self.worker.run_once()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.worker is not None:
            self.worker.stop()
        if self.s3 is not None:
            close = getattr(self.s3, "close", None)
            if callable(close):
                close()
        self.credit_store.close()
        self.billing_store.close()
        if self._owns_pool:
            self._pool.closeall()

    def __enter__(self) -> BursarRuntime:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

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
        if self.clickhouse is not None:
            self.clickhouse.write_usage(usage, outbox_event.event_id)

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
        if self.s3 is None:
            raise RuntimeError("S3 archive is not configured")
        archived = self.s3.archive(event)
        recorded = repository.archive_billing_event_payload(
            event.event_id,
            archived.key,
            archived.version_id,
            self.s3.purge_postgres_payload,
        )
        if not recorded:
            msg = f"Could not record archive pointer for billing event {event.event_id}"
            raise RuntimeError(msg)


def create_bursar_runtime(options: BursarRuntimeOptions) -> BursarRuntime:
    if isinstance(options.postgres, str):
        if not options.postgres.strip():
            msg = "postgres connection string must not be empty"
            raise ValueError(msg)
        pool = psycopg2.pool.ThreadedConnectionPool(1, 10, options.postgres)
        return BursarRuntime(pool, True, options)
    return BursarRuntime(options.postgres, False, options)
