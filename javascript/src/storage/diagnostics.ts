import { z } from "zod";

import { persistedDiagnosticSummary } from "../shared/diagnostics.js";
import type { OutboxRunResult } from "./outbox-worker.js";

export type WorkerLifecycle = "not_configured" | "not_started" | "running" | "stopped";
export type DependencyStatus = "ok" | "error" | "skipped" | "unsupported";

export interface WorkerRunSnapshot {
  source: "manual";
  status: "completed" | "failed";
  startedAt: string;
  completedAt: string;
  result: OutboxRunResult | null;
  error?: string;
}

export interface WorkerErrorSnapshot {
  at: string;
  error: string;
}

export interface WorkerState {
  configured: boolean;
  lifecycle: WorkerLifecycle;
  lastRun: WorkerRunSnapshot | null;
  lastError: WorkerErrorSnapshot | null;
}

export interface BursarRuntimeState {
  ready: boolean;
  /** PostgreSQL-backed accounting and the active catalog are locally usable. */
  financialReady: boolean;
  /** The optional outbox projection worker is either absent or running. */
  projectionReady: boolean;
  /** Financial operations are ready while an optional projection is not. */
  degraded: boolean;
  started: boolean;
  closed: boolean;
  catalogLoaded: boolean;
  worker: WorkerState;
}

export interface DependencyCheck {
  status: DependencyStatus;
  latencyMs: number | null;
  error?: string;
  reason?: string;
}

export interface CatalogDependencyCheck extends DependencyCheck {
  loaded: boolean;
  currentRevisionId: string | null;
  currentRevision: number | null;
}

export interface OutboxStatusSnapshot {
  pendingCount: number;
  processingCount: number;
  deliveredCount: number;
  deadLetterCount: number;
  oldestPendingAt: string | null;
}

export interface OutboxDependencyCheck extends DependencyCheck {
  snapshot: OutboxStatusSnapshot | null;
  limit: number;
}

export interface BursarRuntimeDiagnostics {
  checkedAt: string;
  ready: boolean;
  financialReady: boolean;
  projectionReady: boolean;
  degraded: boolean;
  state: BursarRuntimeState;
  postgres: DependencyCheck;
  catalog: CatalogDependencyCheck;
  outbox: OutboxDependencyCheck;
}

export interface CheckDependenciesOptions {
  /** Bound forwarded to outbox diagnostics when the store supports one. */
  outboxLimit?: number;
}

export interface CatalogRevisionSnapshot {
  id: string;
  version: number;
}

export interface RuntimeDiagnosticsOperations {
  checkPostgres: () => Promise<void>;
  getCatalogRevision: () => Promise<CatalogRevisionSnapshot | null>;
  getOutboxStatus?: (limit: number) => Promise<OutboxStatusSnapshot>;
}

export interface RuntimeStateInput {
  started: boolean;
  closed: boolean;
  catalogLoaded: boolean;
}

const checkDependenciesOptionsSchema = z
  .object({
    outboxLimit: z.number().finite().int().min(1).max(1_000).default(100),
  })
  .strict();

/** Runtime-local state plus active, non-HTTP dependency checks. */
export class RuntimeDiagnosticsTracker {
  private workerStarted = false;
  private workerStopped = false;
  private lastRun: WorkerRunSnapshot | null = null;
  private lastError: WorkerErrorSnapshot | null = null;

  constructor(
    private readonly operations: RuntimeDiagnosticsOperations,
    private readonly workerConfigured: boolean,
  ) {}

  markWorkerStarted(): void {
    if (this.workerConfigured) this.workerStarted = true;
  }

  markWorkerStopped(): void {
    if (this.workerConfigured) this.workerStopped = true;
  }

  recordWorkerError(error: unknown): void {
    this.lastError = {
      at: new Date().toISOString(),
      error: persistedDiagnosticSummary(error, "outbox_worker_failed"),
    };
  }

  async observeManualRun(operation: () => Promise<OutboxRunResult>): Promise<OutboxRunResult> {
    const startedAt = new Date().toISOString();
    try {
      const result = await operation();
      this.lastRun = {
        source: "manual",
        status: "completed",
        startedAt,
        completedAt: new Date().toISOString(),
        result,
      };
      return result;
    } catch (error) {
      const message = persistedDiagnosticSummary(error, "outbox_worker_failed");
      this.lastRun = {
        source: "manual",
        status: "failed",
        startedAt,
        completedAt: new Date().toISOString(),
        result: null,
        error: message,
      };
      this.lastError = { at: this.lastRun.completedAt, error: message };
      throw error;
    }
  }

  state(input: RuntimeStateInput): BursarRuntimeState {
    const financialReady = input.started && !input.closed && input.catalogLoaded;
    const projectionReady =
      !this.workerConfigured || this.workerLifecycle(input.closed) === "running";
    return {
      ready: financialReady && projectionReady,
      financialReady,
      projectionReady,
      degraded: financialReady && !projectionReady,
      started: input.started,
      closed: input.closed,
      catalogLoaded: input.catalogLoaded,
      worker: {
        configured: this.workerConfigured,
        lifecycle: this.workerLifecycle(input.closed),
        lastRun: this.lastRun,
        lastError: this.lastError,
      },
    };
  }

  async checkDependencies(
    input: RuntimeStateInput,
    options: CheckDependenciesOptions = {},
  ): Promise<BursarRuntimeDiagnostics> {
    const parsed = checkDependenciesOptionsSchema.parse(options);
    const state = this.state(input);
    if (input.closed) {
      const skipped = skippedDependency("runtime is closed");
      return {
        checkedAt: new Date().toISOString(),
        ready: false,
        financialReady: false,
        projectionReady: false,
        degraded: false,
        state,
        postgres: skipped,
        catalog: {
          ...skipped,
          loaded: input.catalogLoaded,
          currentRevisionId: null,
          currentRevision: null,
        },
        outbox: {
          ...skipped,
          snapshot: null,
          limit: parsed.outboxLimit,
        },
      };
    }

    const postgres = await checkDependency(this.operations.checkPostgres, "postgres_check_failed");
    const catalog = await this.checkCatalog(input.catalogLoaded);
    const outbox = await this.checkOutbox(parsed.outboxLimit);
    const financialReady =
      state.financialReady && postgres.status === "ok" && catalog.status === "ok";
    const projectionReady =
      state.projectionReady &&
      (!this.workerConfigured ||
        (outbox.status === "ok" && outbox.snapshot?.deadLetterCount === 0));
    return {
      checkedAt: new Date().toISOString(),
      ready: financialReady && projectionReady,
      financialReady,
      projectionReady,
      degraded: financialReady && !projectionReady,
      state,
      postgres,
      catalog,
      outbox,
    };
  }

  private workerLifecycle(closed: boolean): WorkerLifecycle {
    if (!this.workerConfigured) return "not_configured";
    if (closed || this.workerStopped) return "stopped";
    return this.workerStarted ? "running" : "not_started";
  }

  private async checkCatalog(loaded: boolean): Promise<CatalogDependencyCheck> {
    const started = Date.now();
    try {
      const revision = await this.operations.getCatalogRevision();
      validateCatalogRevision(revision);
      return {
        status: "ok",
        latencyMs: Date.now() - started,
        loaded,
        currentRevisionId: revision?.id ?? null,
        currentRevision: revision?.version ?? null,
      };
    } catch (error) {
      return {
        status: "error",
        latencyMs: Date.now() - started,
        loaded,
        currentRevisionId: null,
        currentRevision: null,
        error: persistedDiagnosticSummary(error, "catalog_check_failed"),
      };
    }
  }

  private async checkOutbox(limit: number): Promise<OutboxDependencyCheck> {
    if (!this.operations.getOutboxStatus) {
      return {
        status: "unsupported",
        latencyMs: null,
        snapshot: null,
        limit,
        reason: "the configured outbox store does not expose bounded status",
      };
    }
    const started = Date.now();
    try {
      const snapshot = await this.operations.getOutboxStatus(limit);
      validateOutboxSnapshot(snapshot);
      return {
        status: "ok",
        latencyMs: Date.now() - started,
        snapshot,
        limit,
      };
    } catch (error) {
      return {
        status: "error",
        latencyMs: Date.now() - started,
        snapshot: null,
        limit,
        error: persistedDiagnosticSummary(error, "outbox_check_failed"),
      };
    }
  }
}

async function checkDependency(
  operation: () => Promise<void>,
  fallback: string,
): Promise<DependencyCheck> {
  const started = Date.now();
  try {
    await operation();
    return { status: "ok", latencyMs: Date.now() - started };
  } catch (error) {
    return {
      status: "error",
      latencyMs: Date.now() - started,
      error: persistedDiagnosticSummary(error, fallback),
    };
  }
}

function skippedDependency(reason: string): DependencyCheck {
  return { status: "skipped", latencyMs: null, reason };
}

function validateCatalogRevision(revision: CatalogRevisionSnapshot | null): void {
  if (revision === null) return;
  if (!revision.id || !Number.isSafeInteger(revision.version) || revision.version < 1) {
    throw new TypeError("catalog check returned a malformed revision");
  }
}

function validateOutboxSnapshot(snapshot: OutboxStatusSnapshot): void {
  for (const [key, value] of Object.entries({
    pendingCount: snapshot.pendingCount,
    processingCount: snapshot.processingCount,
    deliveredCount: snapshot.deliveredCount,
    deadLetterCount: snapshot.deadLetterCount,
  })) {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new TypeError(`outbox status field ${key} must be a non-negative integer`);
    }
  }
  if (snapshot.oldestPendingAt !== null && typeof snapshot.oldestPendingAt !== "string") {
    throw new TypeError("outbox status oldestPendingAt must be a timestamp string or null");
  }
}
