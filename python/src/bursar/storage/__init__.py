"""Optional high-volume storage infrastructure.

Importing this module requires the ``postgres`` extra, mirroring the
JavaScript SDK's Node-only storage entry point.
"""

from bursar.storage.adapters import (
    ClickHouseClient,
    ClickHouseQueryResult,
    ClickHouseUsageStore,
    ClickHouseUsageStoreOptions,
    S3BillingArchive,
    S3BillingArchiveOptions,
    S3PutObject,
    S3PutObjectRequest,
    S3PutObjectResult,
)
from bursar.storage.outbox_worker import (
    OutboxRunResult,
    OutboxWorker,
    OutboxWorkerOptions,
)
from bursar.storage.ports import (
    BillingEventPayloadExport,
    BillingPayloadArchive,
    BillingPayloadArchiveResult,
    OutboxEvent,
    OutboxHandler,
    OutboxStore,
    UsageChargeExport,
    UsageEventSink,
)
from bursar.storage.runtime import (
    BursarRuntime,
    BursarRuntimeBursarOptions,
    BursarRuntimeOptions,
    UsageAnalyticsSink,
    create_bursar_runtime,
)

__all__ = [
    "BillingEventPayloadExport",
    "BillingPayloadArchive",
    "BillingPayloadArchiveResult",
    "BursarRuntime",
    "BursarRuntimeBursarOptions",
    "BursarRuntimeOptions",
    "ClickHouseClient",
    "ClickHouseQueryResult",
    "ClickHouseUsageStore",
    "ClickHouseUsageStoreOptions",
    "OutboxEvent",
    "OutboxHandler",
    "OutboxRunResult",
    "OutboxStore",
    "OutboxWorker",
    "OutboxWorkerOptions",
    "S3BillingArchive",
    "S3BillingArchiveOptions",
    "S3PutObject",
    "S3PutObjectRequest",
    "S3PutObjectResult",
    "UsageAnalyticsSink",
    "UsageChargeExport",
    "UsageEventSink",
    "create_bursar_runtime",
]
