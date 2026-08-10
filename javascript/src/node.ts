/**
 * Node.js-only bursar exports.
 *
 * These modules depend on Node built-ins (`crypto`, `fs`) and are not
 * compatible with Edge Runtime environments.  Import them from the
 * ``@zonastery/bursar/node`` subpath when you need Node-specific behaviour:
 *
 * ```ts
 * import { loadConfigFile } from "@zonastery/bursar/node";
 * ```
 */

export { loadConfigFile } from "./load-config-file.js";

// Optional high-volume storage infrastructure. These adapters are Node-only;
// the main package remains the PostgreSQL-first application API.
export { BursarRuntime, createBursarRuntime } from "./storage/runtime.js";
export type {
  BursarRuntimeBursarOptions,
  BursarRuntimeHealth,
  BursarRuntimeOptions,
  BursarRuntimeStartOptions,
} from "./storage/runtime.js";
export type {
  BursarRuntimeDiagnostics,
  BursarRuntimeState,
  CatalogDependencyCheck,
  CheckDependenciesOptions,
  DependencyCheck,
  DependencyStatus,
  OutboxDependencyCheck,
  OutboxStatusSnapshot,
  WorkerErrorSnapshot,
  WorkerLifecycle,
  WorkerRunSnapshot,
  WorkerState,
} from "./storage/diagnostics.js";
export { BursarMaintenance, BursarOperatorMaintenance } from "./storage/maintenance.js";
export type {
  MaintenanceRunOptions,
  MaintenanceRunResult,
  MaintenanceRunStatus,
  MaintenanceOperations,
  MaintenanceTaskResult,
  MaintenanceTaskStatus,
  OperatorMaintenanceRunOptions,
  PartitionMaintenanceResult,
  StorageMaintenanceCounts,
  StorageMaintenanceResult,
  StorageMaintenanceStatus,
  StoragePartition,
} from "./storage/maintenance.js";
export { OutboxWorker } from "./storage/outbox-worker.js";
export type {
  OutboxClaimLossPhase,
  OutboxEventOutcome,
  OutboxEventOutcomeStatus,
  OutboxRunResult,
  OutboxWorkerOptions,
} from "./storage/outbox-worker.js";
export { ClickHouseUsageStore } from "./storage/adapters/clickhouse.js";
export type {
  ClickHouseClient,
  ClickHouseQueryResult,
  ClickHouseUsageStoreOptions,
} from "./storage/adapters/clickhouse.js";
export { S3BillingArchive } from "./storage/adapters/s3.js";
export type {
  S3ArchiveClient,
  S3ArchiveClientFactory,
  S3BillingArchiveOptions,
  S3Credentials,
  S3PutObjectOptions,
} from "./storage/adapters/s3.js";
export type {
  BillingEventPayloadExport,
  BillingPayloadArchive,
  BillingPayloadArchiveResult,
  OutboxClaimRenewalStore,
  OutboxDeadLetter,
  OutboxDeadLetterCursor,
  OutboxDeadLetterListOptions,
  OutboxDeadLetterPage,
  OutboxEvent,
  OutboxHandler,
  OutboxRecoveryStore,
  OutboxStats,
  OutboxStore,
  UsageChargeExport,
  UsageEventSink,
} from "./storage/ports.js";
