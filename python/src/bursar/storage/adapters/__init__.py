"""Optional storage adapters."""

from bursar.storage.adapters.clickhouse import (
    ClickHouseClient,
    ClickHouseQueryResult,
    ClickHouseUsageStore,
    ClickHouseUsageStoreOptions,
)
from bursar.storage.adapters.s3 import (
    S3BillingArchive,
    S3BillingArchiveOptions,
    S3PutObject,
    S3PutObjectRequest,
    S3PutObjectResult,
)

__all__ = [
    "ClickHouseClient",
    "ClickHouseQueryResult",
    "ClickHouseUsageStore",
    "ClickHouseUsageStoreOptions",
    "S3BillingArchive",
    "S3BillingArchiveOptions",
    "S3PutObject",
    "S3PutObjectRequest",
    "S3PutObjectResult",
]
