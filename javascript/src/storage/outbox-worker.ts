import { z } from "zod";

import { persistedDiagnosticSummary } from "../shared/diagnostics.js";
import type { OutboxEvent, OutboxHandler, OutboxStore } from "./ports.js";

export type OutboxEventOutcomeStatus = "delivered" | "delivery_failed" | "claim_lost";
export type OutboxClaimLossPhase = "heartbeat" | "complete" | "fail";

export interface OutboxEventOutcome {
  topic: string;
  attemptCount: number;
  status: OutboxEventOutcomeStatus;
  /** Persistence-safe diagnostic summary; never a raw exception message. */
  summary: string | null;
  durationMs: number;
  retryDelaySeconds: number | null;
  deadLettered: boolean;
  claimLossPhase: OutboxClaimLossPhase | null;
}

export interface OutboxWorkerOptions {
  batchSize?: number;
  concurrency?: number;
  leaseSeconds?: number;
  pollIntervalMs?: number;
  retryDelaySeconds?: number;
  maxRetryDelaySeconds?: number;
  attemptLimit?: number;
  onError?: (error: unknown) => void | Promise<void>;
  onEventOutcome?: (outcome: OutboxEventOutcome) => void | Promise<void>;
}

export interface OutboxRunResult {
  claimed: number;
  delivered: number;
  failed: number;
  /** Events that lost or could not prove ownership. */
  claimLost: number;
}

const outboxWorkerOptionsSchema = z
  .object({
    batchSize: z.number().finite().int().min(1).max(1_000).default(100),
    concurrency: z.number().finite().int().min(1).max(100).default(4),
    leaseSeconds: z.number().finite().int().min(1).max(3_600).default(60),
    pollIntervalMs: z.number().finite().int().min(10).max(3_600_000).default(1_000),
    retryDelaySeconds: z.number().finite().int().min(1).max(86_400).default(30),
    maxRetryDelaySeconds: z.number().finite().int().min(1).max(86_400).default(3_600),
    attemptLimit: z.number().finite().int().min(1).max(100).default(10),
    onError: z
      .custom<(error: unknown) => void | Promise<void>>(
        (value) => typeof value === "function",
        "onError must be a function",
      )
      .optional(),
    onEventOutcome: z
      .custom<(outcome: OutboxEventOutcome) => void | Promise<void>>(
        (value) => typeof value === "function",
        "onEventOutcome must be a function",
      )
      .optional(),
  })
  .strict()
  .refine((options) => options.maxRetryDelaySeconds >= options.retryDelaySeconds, {
    path: ["maxRetryDelaySeconds"],
    message: "must be at least retryDelaySeconds",
  });

type NormalizedWorkerOptions = Omit<
  z.infer<typeof outboxWorkerOptionsSchema>,
  "onError" | "onEventOutcome"
> & {
  onError: ((error: unknown) => void | Promise<void>) | null;
  onEventOutcome: ((outcome: OutboxEventOutcome) => void | Promise<void>) | null;
};

interface ClaimHeartbeatResult {
  claimLost: boolean;
  summary: string | null;
}

interface ClaimHeartbeat {
  stop(): Promise<ClaimHeartbeatResult>;
}

/**
 * Generic leased-outbox dispatcher.
 *
 * Handlers must be idempotent because a process can stop after the external
 * write succeeds but before PostgreSQL records the acknowledgement.
 */
export class OutboxWorker {
  private readonly store: OutboxStore;
  private readonly handlers = new Map<string, OutboxHandler[]>();
  private readonly topics: string[];
  private readonly options: NormalizedWorkerOptions;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private activeRun: Promise<OutboxRunResult> | null = null;
  private started = false;
  private stopped = false;

  constructor(
    store: OutboxStore,
    handlers: readonly OutboxHandler[],
    options: OutboxWorkerOptions = {},
  ) {
    if (handlers.length === 0) throw new TypeError("OutboxWorker requires at least one handler");
    if (typeof store.renew !== "function") {
      throw new TypeError("OutboxWorker store must support claim renewal");
    }
    const parsedOptions = outboxWorkerOptionsSchema.parse(options);
    this.store = store;
    for (const handler of handlers) {
      if (handler.topics.length === 0) {
        throw new TypeError("Outbox handlers must declare at least one topic");
      }
      for (const topic of handler.topics) {
        if (!topic.trim()) throw new TypeError("Outbox topics must not be empty");
        const topicHandlers = this.handlers.get(topic) ?? [];
        topicHandlers.push(handler);
        this.handlers.set(topic, topicHandlers);
      }
    }
    this.topics = [...this.handlers.keys()].sort();
    this.options = {
      ...parsedOptions,
      onError: parsedOptions.onError ?? null,
      onEventOutcome: parsedOptions.onEventOutcome ?? null,
    };
  }

  async start(): Promise<void> {
    if (this.started) return;
    if (this.stopped) throw new Error("OutboxWorker cannot be restarted after stop");
    this.started = true;
    this.schedule(0);
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.activeRun) await this.activeRun;
  }

  runOnce(): Promise<OutboxRunResult> {
    if (this.stopped) throw new Error("OutboxWorker has been stopped");
    if (!this.activeRun) {
      this.activeRun = this.dispatchOnce().finally(() => {
        this.activeRun = null;
      });
    }
    return this.activeRun;
  }

  private schedule(delayMs: number): void {
    if (this.stopped) return;
    this.timer = setTimeout(() => {
      void this.runOnce()
        .catch((error: unknown) => this.reportError(error))
        .finally(() => this.schedule(this.options.pollIntervalMs));
    }, delayMs);
    this.timer.unref?.();
  }

  private reportError(error: unknown): void {
    try {
      const result = this.options.onError?.(error);
      if (result) void Promise.resolve(result).catch(() => {});
    } catch {
      // Observability callbacks must never stop the delivery loop.
    }
  }

  private reportOutcome(outcome: OutboxEventOutcome): void {
    try {
      const result = this.options.onEventOutcome?.(outcome);
      if (result) void Promise.resolve(result).catch(() => {});
    } catch {
      // Per-event observers must never stop this or a later delivery.
    }
  }

  private async dispatchOnce(): Promise<OutboxRunResult> {
    const result: OutboxRunResult = { claimed: 0, delivered: 0, failed: 0, claimLost: 0 };
    let remainingBudget = this.options.batchSize;

    while (remainingBudget > 0) {
      const availableSlots = Math.min(this.options.concurrency, remainingBudget);
      const events = await this.store.claim(this.topics, availableSlots, this.options.leaseSeconds);
      if (events.length === 0) break;
      if (events.length > availableSlots) {
        throw new Error(
          `Outbox store returned ${events.length} events for ${availableSlots} available slots`,
        );
      }

      result.claimed += events.length;
      remainingBudget -= events.length;
      const outcomes = await Promise.all(events.map((event) => this.dispatchEvent(event)));
      for (const outcome of outcomes) {
        if (outcome === "delivered") result.delivered += 1;
        else {
          result.failed += 1;
          if (outcome === "claim_lost") result.claimLost += 1;
        }
      }
      if (events.length < availableSlots) break;
    }

    return result;
  }

  private async dispatchEvent(event: OutboxEvent): Promise<OutboxEventOutcomeStatus> {
    const startedAt = Date.now();
    const heartbeat = this.startHeartbeat(event);
    let deliveryFailure: { error: unknown } | null = null;
    try {
      const handlers = this.handlers.get(event.topic);
      if (!handlers?.length) throw new Error(`No handler for outbox topic ${event.topic}`);
      await Promise.all(handlers.map((handler) => handler.handle(event)));
    } catch (error) {
      deliveryFailure = { error };
    }

    const heartbeatResult = await heartbeat.stop();
    if (heartbeatResult.claimLost) {
      return this.claimLost(event, "heartbeat", heartbeatResult.summary, startedAt);
    }
    if (deliveryFailure) {
      return this.failDelivery(event, deliveryFailure.error, startedAt);
    }

    try {
      if (!(await this.store.complete(event))) {
        return this.claimLost(
          event,
          "complete",
          persistedDiagnosticSummary(null, "outbox_claim_lost"),
          startedAt,
        );
      }
    } catch (error) {
      return this.claimLost(
        event,
        "complete",
        persistedDiagnosticSummary(error, "outbox_claim_lost"),
        startedAt,
      );
    }

    this.reportOutcome({
      topic: event.topic,
      attemptCount: event.attemptCount,
      status: "delivered",
      summary: null,
      durationMs: Math.max(Date.now() - startedAt, 0),
      retryDelaySeconds: null,
      deadLettered: false,
      claimLossPhase: null,
    });
    return "delivered";
  }

  private async failDelivery(
    event: OutboxEvent,
    error: unknown,
    startedAt: number,
  ): Promise<OutboxEventOutcomeStatus> {
    const summary = persistedDiagnosticSummary(error, "outbox_delivery_failed");
    const exponentialDelay =
      this.options.retryDelaySeconds * 2 ** Math.max(event.attemptCount - 1, 0);
    const retryDelay = Math.min(exponentialDelay, this.options.maxRetryDelaySeconds);

    try {
      if (!(await this.store.fail(event, summary, retryDelay, this.options.attemptLimit))) {
        return this.claimLost(event, "fail", summary, startedAt);
      }
    } catch (failureError) {
      return this.claimLost(
        event,
        "fail",
        persistedDiagnosticSummary(failureError, "outbox_claim_lost"),
        startedAt,
      );
    }

    this.reportOutcome({
      topic: event.topic,
      attemptCount: event.attemptCount,
      status: "delivery_failed",
      summary,
      durationMs: Math.max(Date.now() - startedAt, 0),
      retryDelaySeconds: retryDelay,
      deadLettered: event.attemptCount >= this.options.attemptLimit,
      claimLossPhase: null,
    });
    return "delivery_failed";
  }

  private claimLost(
    event: OutboxEvent,
    phase: OutboxClaimLossPhase,
    summary: string | null,
    startedAt: number,
  ): OutboxEventOutcomeStatus {
    this.reportOutcome({
      topic: event.topic,
      attemptCount: event.attemptCount,
      status: "claim_lost",
      summary: summary ?? persistedDiagnosticSummary(null, "outbox_claim_lost"),
      durationMs: Math.max(Date.now() - startedAt, 0),
      retryDelaySeconds: null,
      deadLettered: false,
      claimLossPhase: phase,
    });
    return "claim_lost";
  }

  private startHeartbeat(event: OutboxEvent): ClaimHeartbeat {
    const intervalMs = Math.max(100, Math.floor((this.options.leaseSeconds * 1_000) / 3));
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pending: Promise<void> | null = null;
    let stopped = false;
    let claimLost = false;
    let heartbeatSummary: string | null = null;

    const loseClaim = (error: unknown): void => {
      claimLost = true;
      heartbeatSummary = persistedDiagnosticSummary(error, "outbox_claim_lost");
      if (timer) clearTimeout(timer);
      timer = null;
    };

    const schedule = (): void => {
      if (stopped || claimLost) return;
      timer = setTimeout(() => {
        timer = null;
        pending = Promise.resolve()
          .then(async () => {
            if (!(await this.store.renew(event, this.options.leaseSeconds))) {
              loseClaim("outbox_claim_lost");
            }
          })
          .catch((error: unknown) => loseClaim(error))
          .finally(() => {
            pending = null;
            schedule();
          });
      }, intervalMs);
      timer.unref?.();
    };

    schedule();
    return {
      stop: async () => {
        stopped = true;
        if (timer) clearTimeout(timer);
        timer = null;
        if (pending) await pending;
        return { claimLost, summary: heartbeatSummary };
      },
    };
  }
}
