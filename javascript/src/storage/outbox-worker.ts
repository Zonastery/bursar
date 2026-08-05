import { boundedDiagnosticMessage } from "../shared/diagnostics.js";
import type { OutboxEvent, OutboxHandler, OutboxStore } from "./ports.js";

export interface OutboxWorkerOptions {
  batchSize?: number;
  concurrency?: number;
  leaseSeconds?: number;
  pollIntervalMs?: number;
  retryDelaySeconds?: number;
  maxRetryDelaySeconds?: number;
  attemptLimit?: number;
  onError?: (error: unknown) => void;
}

export interface OutboxRunResult {
  claimed: number;
  delivered: number;
  failed: number;
}

interface NormalizedWorkerOptions {
  batchSize: number;
  concurrency: number;
  leaseSeconds: number;
  pollIntervalMs: number;
  retryDelaySeconds: number;
  maxRetryDelaySeconds: number;
  attemptLimit: number;
  onError: ((error: unknown) => void) | null;
}

function integerOption(
  value: number | undefined,
  fallback: number,
  name: string,
  minimum: number,
  maximum: number,
): number {
  const normalized = value ?? fallback;
  if (!Number.isInteger(normalized) || normalized < minimum || normalized > maximum) {
    throw new RangeError(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
  return normalized;
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
      batchSize: integerOption(options.batchSize, 100, "batchSize", 1, 1_000),
      concurrency: integerOption(options.concurrency, 4, "concurrency", 1, 100),
      leaseSeconds: integerOption(options.leaseSeconds, 60, "leaseSeconds", 1, 3_600),
      pollIntervalMs: integerOption(options.pollIntervalMs, 1_000, "pollIntervalMs", 10, 3_600_000),
      retryDelaySeconds: integerOption(
        options.retryDelaySeconds,
        30,
        "retryDelaySeconds",
        1,
        86_400,
      ),
      maxRetryDelaySeconds: integerOption(
        options.maxRetryDelaySeconds,
        3_600,
        "maxRetryDelaySeconds",
        1,
        86_400,
      ),
      attemptLimit: integerOption(options.attemptLimit, 10, "attemptLimit", 1, 1_000),
      onError: options.onError ?? null,
    };
    if (this.options.maxRetryDelaySeconds < this.options.retryDelaySeconds) {
      throw new RangeError("maxRetryDelaySeconds must be at least retryDelaySeconds");
    }
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
        .catch((error: unknown) => this.options.onError?.(error))
        .finally(() => this.schedule(this.options.pollIntervalMs));
    }, delayMs);
    this.timer.unref?.();
  }

  private async dispatchOnce(): Promise<OutboxRunResult> {
    const events = await this.store.claim(
      this.topics,
      this.options.batchSize,
      this.options.leaseSeconds,
    );
    const result: OutboxRunResult = { claimed: events.length, delivered: 0, failed: 0 };
    let cursor = 0;
    const consume = async (): Promise<void> => {
      while (cursor < events.length) {
        const event = events[cursor++];
        if (!event) return;
        const delivered = await this.dispatchEvent(event);
        if (delivered) result.delivered += 1;
        else result.failed += 1;
      }
    };
    const consumers = Math.min(this.options.concurrency, events.length);
    await Promise.all(Array.from({ length: consumers }, () => consume()));
    return result;
  }

  private async dispatchEvent(event: OutboxEvent): Promise<boolean> {
    try {
      const handlers = this.handlers.get(event.topic);
      if (!handlers?.length) throw new Error(`No handler for outbox topic ${event.topic}`);
      await Promise.all(handlers.map((handler) => handler.handle(event)));
      if (!(await this.store.complete(event))) {
        throw new Error(`Lost outbox claim for event ${event.eventId}`);
      }
      return true;
    } catch (error) {
      const message = boundedDiagnosticMessage(
        error instanceof Error ? `${error.name}: ${error.message}` : error,
        "outbox_delivery_failed",
      );
      const exponentialDelay =
        this.options.retryDelaySeconds * 2 ** Math.max(event.attemptCount - 1, 0);
      const retryDelay = Math.min(exponentialDelay, this.options.maxRetryDelaySeconds);
      await this.store.fail(event, message, retryDelay, this.options.attemptLimit);
      return false;
    }
  }
}
