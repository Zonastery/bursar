import { describe, expect, it, vi } from "vitest";

import { OutboxWorker, type OutboxEventOutcome } from "../src/storage/outbox-worker.js";
import type { OutboxEvent, OutboxStore } from "../src/storage/ports.js";
import { PostgresStorageRepository } from "../src/storage/postgres-repository.js";

const TENANT_ID = "00000000-0000-0000-0000-000000000001";

function event(id: number, attemptCount = 1): OutboxEvent {
  return {
    eventId: String(id),
    tenantId: TENANT_ID,
    topic: "usage.charge_recorded",
    aggregateType: "credit_usage_charge",
    aggregateId: `00000000-0000-0000-0000-${String(id).padStart(12, "0")}`,
    payloadVersion: 1,
    payload: { secret: "must-not-reach-outcome-hooks" },
    claimToken: `10000000-0000-0000-0000-${String(id).padStart(12, "0")}`,
    attemptCount,
    createdAt: "2026-08-10T00:00:00.000Z",
  };
}

class FakeStore implements OutboxStore {
  readonly claimLimits: number[] = [];
  readonly completed: OutboxEvent[] = [];
  readonly failed: Array<{
    event: OutboxEvent;
    summary: string;
    retryDelay: number;
    limit: number;
  }> = [];
  readonly renewed: OutboxEvent[] = [];
  completeResult = true;
  failResult = true;
  renewResult = true;

  constructor(private readonly queue: OutboxEvent[]) {}

  async claim(
    _topics: readonly string[],
    limit: number,
    _leaseSeconds: number,
  ): Promise<OutboxEvent[]> {
    this.claimLimits.push(limit);
    return this.queue.splice(0, limit);
  }

  async renew(outboxEvent: OutboxEvent): Promise<boolean> {
    this.renewed.push(outboxEvent);
    return this.renewResult;
  }

  async complete(outboxEvent: OutboxEvent): Promise<boolean> {
    this.completed.push(outboxEvent);
    return this.completeResult;
  }

  async fail(
    outboxEvent: OutboxEvent,
    summary: string,
    retryDelaySeconds: number,
    attemptLimit: number,
  ): Promise<boolean> {
    this.failed.push({
      event: outboxEvent,
      summary,
      retryDelay: retryDelaySeconds,
      limit: attemptLimit,
    });
    return this.failResult;
  }
}

const handler = (handle: (outboxEvent: OutboxEvent) => Promise<void> | void) => ({
  topics: ["usage.charge_recorded"],
  handle: async (outboxEvent: OutboxEvent) => {
    await handle(outboxEvent);
  },
});

describe("OutboxWorker correctness", () => {
  it("requires claim renewal support at construction", () => {
    const store = new FakeStore([]);
    Object.defineProperty(store, "renew", { value: undefined });
    expect(() => new OutboxWorker(store, [handler(vi.fn())])).toThrow(/claim renewal/);
  });

  it("claims only available concurrency slots while honoring the run budget", async () => {
    const store = new FakeStore([event(1), event(2), event(3), event(4), event(5)]);
    const worker = new OutboxWorker(store, [handler(vi.fn())], {
      batchSize: 5,
      concurrency: 2,
    });

    await expect(worker.runOnce()).resolves.toEqual({
      claimed: 5,
      delivered: 5,
      failed: 0,
      claimLost: 0,
    });
    expect(store.claimLimits).toEqual([2, 2, 1]);
  });

  it("aligns attemptLimit with PostgreSQL's maximum of 100", async () => {
    expect(
      () => new OutboxWorker(new FakeStore([]), [handler(vi.fn())], { attemptLimit: 101 }),
    ).toThrow(/attemptLimit/);
    const worker = new OutboxWorker(new FakeStore([]), [handler(vi.fn())], {
      attemptLimit: 100,
    });
    await expect(worker.runOnce()).resolves.toEqual({
      claimed: 0,
      delivered: 0,
      failed: 0,
      claimLost: 0,
    });
  });

  it("renews a claim while its handler remains active and stops afterward", async () => {
    vi.useFakeTimers();
    try {
      const store = new FakeStore([event(1)]);
      let releaseHandler: (() => void) | undefined;
      const blocked = new Promise<void>((resolve) => {
        releaseHandler = resolve;
      });
      const worker = new OutboxWorker(store, [handler(() => blocked)], {
        leaseSeconds: 1,
      });

      const run = worker.runOnce();
      await vi.advanceTimersByTimeAsync(334);
      expect(store.renewed).toHaveLength(1);
      releaseHandler?.();

      await expect(run).resolves.toEqual({
        claimed: 1,
        delivered: 1,
        failed: 0,
        claimLost: 0,
      });
      await vi.advanceTimersByTimeAsync(1_000);
      expect(store.renewed).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces complete=false as claim loss without attempting failure", async () => {
    const store = new FakeStore([event(1, 3)]);
    store.completeResult = false;
    const outcomes: OutboxEventOutcome[] = [];
    const worker = new OutboxWorker(store, [handler(vi.fn())], {
      onEventOutcome: (outcome) => {
        outcomes.push(outcome);
      },
    });

    await expect(worker.runOnce()).resolves.toEqual({
      claimed: 1,
      delivered: 0,
      failed: 1,
      claimLost: 1,
    });
    expect(store.failed).toEqual([]);
    expect(outcomes).toEqual([
      expect.objectContaining({
        topic: "usage.charge_recorded",
        attemptCount: 3,
        status: "claim_lost",
        summary: "outbox_claim_lost:UnknownError",
        claimLossPhase: "complete",
      }),
    ]);
    expect(outcomes[0]).not.toHaveProperty("event");
    expect(outcomes[0]).not.toHaveProperty("payload");
    expect(outcomes[0]).not.toHaveProperty("claimToken");
  });

  it("passes only a persistence-safe summary and classifies fail=false as claim loss", async () => {
    const store = new FakeStore([event(1, 2)]);
    store.failResult = false;
    const outcomes: OutboxEventOutcome[] = [];
    const worker = new OutboxWorker(
      store,
      [
        handler(() => {
          throw new Error("secret=https://sink.invalid/token/abc");
        }),
      ],
      {
        onEventOutcome: (outcome) => {
          outcomes.push(outcome);
        },
      },
    );

    await expect(worker.runOnce()).resolves.toMatchObject({ claimLost: 1, failed: 1 });
    expect(store.failed[0]?.summary).toBe("outbox_delivery_failed:Error");
    expect(store.failed[0]?.summary).not.toContain("secret");
    expect(outcomes[0]).toMatchObject({
      status: "claim_lost",
      summary: "outbox_delivery_failed:Error",
      claimLossPhase: "fail",
    });
  });

  it("isolates throwing outcome callbacks from every delivery", async () => {
    const store = new FakeStore([event(1), event(2)]);
    const callback = vi.fn(() => {
      throw new Error("observer failed");
    });
    const worker = new OutboxWorker(store, [handler(vi.fn())], {
      batchSize: 2,
      concurrency: 2,
      onEventOutcome: callback,
    });

    await expect(worker.runOnce()).resolves.toEqual({
      claimed: 2,
      delivered: 2,
      failed: 0,
      claimLost: 0,
    });
    expect(callback).toHaveBeenCalledTimes(2);
  });
});

describe("PostgresStorageRepository recovery", () => {
  it("rejects raw failure text before a custom query can persist it", async () => {
    const query = vi.fn().mockResolvedValue([{ fail_tenant_outbox_event: true }]);
    const repository = new PostgresStorageRepository(query, TENANT_ID);

    await expect(repository.fail(event(1), "secret sink response", 30, 10)).rejects.toThrow(
      /summary/,
    );
    expect(query).not.toHaveBeenCalled();

    await expect(
      repository.fail(event(1), "outbox_delivery_failed:TypeError", 30, 10),
    ).resolves.toBe(true);
  });

  it("maps stats and bounded dead-letter cursor pages", async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce([
        {
          pending_count: "2",
          processing_count: "1",
          delivered_count: "3",
          dead_letter_count: "2",
          oldest_pending_at: "2026-08-10T00:00:00.000Z",
        },
      ])
      .mockResolvedValueOnce([
        {
          event_id: "10",
          tenant_id: TENANT_ID,
          topic: "usage.charge_recorded",
          aggregate_type: "credit_usage_charge",
          aggregate_id: "00000000-0000-0000-0000-000000000010",
          payload_version: 1,
          attempt_count: 10,
          last_error: "outbox_delivery_failed:Error",
          created_at: "2026-08-10T00:00:00.000Z",
          updated_at: "2026-08-10T00:01:00.000Z",
        },
        {
          event_id: "11",
          tenant_id: TENANT_ID,
          topic: "usage.charge_recorded",
          aggregate_type: "credit_usage_charge",
          aggregate_id: "00000000-0000-0000-0000-000000000011",
          payload_version: 1,
          attempt_count: 10,
          last_error: "outbox_delivery_failed:Error",
          created_at: "2026-08-10T00:02:00.000Z",
          updated_at: "2026-08-10T00:03:00.000Z",
        },
      ]);
    const repository = new PostgresStorageRepository(query, TENANT_ID);

    await expect(repository.stats()).resolves.toEqual({
      pendingCount: 2,
      processingCount: 1,
      deliveredCount: 3,
      deadLetterCount: 2,
      oldestPendingAt: "2026-08-10T00:00:00.000Z",
    });
    await expect(repository.listDeadLetters({ limit: 1 })).resolves.toEqual({
      items: [
        expect.objectContaining({ eventId: "10", lastError: "outbox_delivery_failed:Error" }),
      ],
      nextCursor: { createdAt: "2026-08-10T00:00:00.000Z", eventId: "10" },
    });
  });
});
