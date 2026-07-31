/**
 * Node.js-only bursar exports.
 *
 * These modules depend on Node built-ins (`crypto`, `fs`) and are not
 * compatible with Edge Runtime environments.  Import them from the
 * ``@zonastery/bursar/node`` subpath when you need Node-specific behaviour:
 *
 * ```ts
 * import { loadPricingFile } from "@zonastery/bursar/node";
 * ```
 */

export { loadPricingFile } from "./load-pricing-file.js";

// Optional high-volume storage infrastructure. These adapters are Node-only;
// the main package remains the PostgreSQL-first application API.
export { BursarRuntime, createBursarRuntime } from "./storage/runtime.js";
export type {
  BursarRuntimeHealth,
  BursarRuntimeOptions,
  BursarRuntimeStartOptions,
} from "./storage/runtime.js";
export { OutboxWorker } from "./storage/outbox-worker.js";
export type { OutboxRunResult, OutboxWorkerOptions } from "./storage/outbox-worker.js";
export { ClickHouseUsageStore } from "./storage/adapters/clickhouse.js";
export type {
  ClickHouseClient,
  ClickHouseQueryResult,
  ClickHouseUsageStoreOptions,
} from "./storage/adapters/clickhouse.js";
export { S3BillingArchive } from "./storage/adapters/s3.js";
export type { S3BillingArchiveOptions, S3Credentials } from "./storage/adapters/s3.js";
export type {
  BillingEventPayloadExport,
  BillingPayloadArchive,
  BillingPayloadArchiveResult,
  OutboxEvent,
  OutboxHandler,
  OutboxStore,
  UsageChargeExport,
  UsageEventSink,
} from "./storage/ports.js";
