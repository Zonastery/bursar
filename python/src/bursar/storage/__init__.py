"""Optional high-volume storage infrastructure.

Importing this module requires the ``postgres`` extra, mirroring the
JavaScript SDK's Node-only storage entry point. Using the native S3 adapter
additionally requires the ``s3`` extra.
"""

from bursar.storage.adapters import (
    ClickHouseClient,
    ClickHouseQueryResult,
    ClickHouseUsageStore,
    ClickHouseUsageStoreOptions,
    S3BillingArchive,
    S3BillingArchiveOptions,
    S3Credentials,
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
    BursarRuntimeHealth,
    BursarRuntimeOptions,
    BursarRuntimeStartOptions,
    UsageAnalyticsSink,
    create_bursar_runtime,
)

__all__ = [
    "BillingEventPayloadExport",
    "BillingPayloadArchive",
    "BillingPayloadArchiveResult",
    "BursarRuntime",
    "BursarRuntimeHealth",
    "BursarRuntimeBursarOptions",
    "BursarRuntimeOptions",
    "BursarRuntimeStartOptions",
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
    "S3Credentials",
    "UsageAnalyticsSink",
    "UsageChargeExport",
    "UsageEventSink",
    "create_bursar_runtime",
]
