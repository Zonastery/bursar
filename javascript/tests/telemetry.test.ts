import { readFileSync } from "node:fs";

import {
  metrics,
  SpanStatusCode,
  trace,
  type Attributes,
  type Meter,
  type Span,
  type Tracer,
} from "@opentelemetry/api";
import { Decimal } from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";
import { StoreTimeoutError } from "../src/errors.js";
import { PostgresClient, type PostgresPool } from "../src/shared/postgres-client.js";
import {
  BURSAR_INSTRUMENTATION_SCOPE,
  BURSAR_INSTRUMENTATION_VERSION,
  getDefaultInstrumentation,
  NOOP_INSTRUMENTATION,
  sanitizeTelemetryAttributes,
  setDefaultInstrumentation,
  telemetryErrorAttributes,
  type Instrumentation,
  type TelemetryAttributes,
} from "../src/telemetry/index.js";
import {
  createOpenTelemetryInstrumentation,
  OpenTelemetryInstrumentation,
} from "../src/telemetry/opentelemetry.js";

const expectedOperations = JSON.parse(
  readFileSync(new URL("../../tests/parity/telemetry_operations.json", import.meta.url), "utf8"),
) as string[];

class RecordingInstrumentation implements Instrumentation {
  readonly operations: Array<{ operation: string; attributes?: TelemetryAttributes }> = [];

  async run<T>(
    operation: string,
    attributes: TelemetryAttributes | undefined,
    callback: () => Promise<T>,
  ): Promise<T> {
    this.operations.push({ operation, ...(attributes === undefined ? {} : { attributes }) });
    return callback();
  }
}

function makeOpenTelemetryDoubles() {
  const captured = {
    active: false,
    spanName: "",
    spanAttributes: {} as Record<string, unknown>,
    statusCodes: [] as number[],
    ended: 0,
    counter: [] as Array<{ value: number; attributes?: Attributes }>,
    histogram: [] as Array<{ value: number; attributes?: Attributes }>,
  };
  const span = {
    setAttributes(attributes: Attributes) {
      Object.assign(captured.spanAttributes, attributes);
      return this;
    },
    setStatus(status: { code: number }) {
      captured.statusCodes.push(status.code);
      return this;
    },
    end() {
      captured.ended += 1;
    },
  } as unknown as Span;
  const tracer = {
    async startActiveSpan<T>(
      name: string,
      options: { attributes?: Attributes },
      callback: (activeSpan: Span) => Promise<T>,
    ): Promise<T> {
      captured.spanName = name;
      Object.assign(captured.spanAttributes, options.attributes);
      captured.active = true;
      try {
        return await callback(span);
      } finally {
        captured.active = false;
      }
    },
  } as unknown as Tracer;
  const meter = {
    createCounter() {
      return {
        add(value: number, attributes?: Attributes) {
          captured.counter.push({ value, attributes });
        },
      };
    },
    createHistogram() {
      return {
        record(value: number, attributes?: Attributes) {
          captured.histogram.push({ value, attributes });
        },
      };
    },
  } as unknown as Meter;
  return { captured, meter, tracer };
}

describe("vendor-neutral telemetry", () => {
  it("never trusts arbitrary error names or codes", () => {
    const failure = Object.assign(new Error("private"), {
      name: "customer_12345",
      code: "tenant_67890",
    });

    expect(telemetryErrorAttributes(failure)).toEqual({ "error.type": "error" });
  });

  it("restores nested defaults correctly when callbacks run out of order", () => {
    const first = new RecordingInstrumentation();
    const second = new RecordingInstrumentation();
    const restoreFirst = setDefaultInstrumentation(first);
    const restoreSecond = setDefaultInstrumentation(second);

    restoreFirst();
    expect(getDefaultInstrumentation()).toBe(second);
    restoreSecond();
    expect(getDefaultInstrumentation()).toBe(NOOP_INSTRUMENTATION);
  });

  it("is a no-op on success and preserves the original failure", async () => {
    await expect(
      NOOP_INSTRUMENTATION.run("credits.reserve", undefined, async () => "ok"),
    ).resolves.toBe("ok");

    const failure = new Error("private failure text");
    await expect(
      NOOP_INSTRUMENTATION.run("credits.reserve", undefined, async () => {
        throw failure;
      }),
    ).rejects.toBe(failure);
  });

  it("allows only bounded low-cardinality attributes", () => {
    expect(
      sanitizeTelemetryAttributes({
        "bursar.backend": "Postgres / Primary",
        "bursar.provider": "Dodo Payments",
        "bursar.outcome": "SUCCESS",
        "tenant.id": "tenant-secret",
        userId: "user-secret",
        idempotencyKey: "request-secret",
        metadata: { prompt: "private prompt" },
        "error.message": "private error",
      }),
    ).toEqual({
      "bursar.backend": "postgres_primary",
      "bursar.provider": "dodo_payments",
      "bursar.outcome": "success",
    });
  });

  it("keeps the instrumentation version synchronized with package.json", () => {
    const packageJson = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8"),
    ) as { name: string; version: string };
    expect(BURSAR_INSTRUMENTATION_SCOPE).toBe(packageJson.name);
    expect(BURSAR_INSTRUMENTATION_VERSION).toBe(packageJson.version);
  });
});

describe("OpenTelemetry API adapter", () => {
  it("records success and keeps the host context active for the callback", async () => {
    const { captured, meter, tracer } = makeOpenTelemetryDoubles();
    const instrumentation = new OpenTelemetryInstrumentation({ meter, tracer });

    await expect(
      instrumentation.run(
        "credits.reserve",
        {
          "bursar.backend": "postgres",
          "tenant.id": "tenant-secret",
        },
        async () => {
          expect(captured.active).toBe(true);
          return "reserved";
        },
      ),
    ).resolves.toBe("reserved");

    expect(captured.spanName).toBe("bursar.credits.reserve");
    expect(captured.spanAttributes).toEqual({
      "bursar.operation": "credits.reserve",
      "bursar.backend": "postgres",
      "bursar.outcome": "success",
    });
    expect(captured.statusCodes).toEqual([SpanStatusCode.OK]);
    expect(captured.counter).toHaveLength(1);
    expect(captured.histogram).toHaveLength(1);
    expect(captured.ended).toBe(1);
    expect(JSON.stringify(captured)).not.toContain("tenant-secret");
  });

  it("records normalized error type/code without the raw message", async () => {
    const { captured, meter, tracer } = makeOpenTelemetryDoubles();
    const instrumentation = new OpenTelemetryInstrumentation({ meter, tracer });
    const failure = new StoreTimeoutError("database URL and tenant are private");

    await expect(
      instrumentation.run("postgres.rpc", { "bursar.backend": "postgres" }, async () => {
        throw failure;
      }),
    ).rejects.toBe(failure);

    expect(captured.spanAttributes).toMatchObject({
      "bursar.operation": "postgres.rpc",
      "bursar.backend": "postgres",
      "bursar.outcome": "error",
      "error.type": "bursar_error",
      "error.code": "store_timeout",
    });
    expect(captured.statusCodes).toEqual([SpanStatusCode.ERROR]);
    expect(JSON.stringify(captured)).not.toContain("database URL and tenant are private");
  });

  it("uses API no-op providers when no SDK is registered", async () => {
    const tracer = vi.spyOn(trace, "getTracer");
    const meter = vi.spyOn(metrics, "getMeter");
    const instrumentation = createOpenTelemetryInstrumentation();

    await expect(
      instrumentation.run("credits.release", undefined, async () => "released"),
    ).resolves.toBe("released");

    expect(tracer).toHaveBeenCalledWith(
      BURSAR_INSTRUMENTATION_SCOPE,
      BURSAR_INSTRUMENTATION_VERSION,
    );
    expect(meter).toHaveBeenCalledWith(
      BURSAR_INSTRUMENTATION_SCOPE,
      BURSAR_INSTRUMENTATION_VERSION,
    );
    vi.restoreAllMocks();
  });
});

describe("instrumented Bursar boundaries", () => {
  it("distinguishes PostgreSQL query and RPC boundaries without SQL attributes", async () => {
    const instrumentation = new RecordingInstrumentation();
    const pool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn(),
    } as unknown as PostgresPool;
    const client = new PostgresClient(pool, { instrumentation });

    await client.query("SELECT 1");
    await client.query("SELECT * FROM bursar.post_credit($1)", ["private-id"]);

    expect(instrumentation.operations).toEqual([
      { operation: "postgres.query", attributes: { "bursar.backend": "postgres" } },
      { operation: "postgres.rpc", attributes: { "bursar.backend": "postgres" } },
    ]);
    expect(
      expectedOperations.filter((operation) => operation.startsWith("postgres.")).sort(),
    ).toEqual(["postgres.query", "postgres.rpc"]);
    expect(JSON.stringify(instrumentation.operations)).not.toContain("private-id");
  });

  it("uses the shared operation contract for explicit credit instrumentation", async () => {
    const instrumentation = new RecordingInstrumentation();
    const failure = new Error("stop after entering operation");
    const throwingStore = new Proxy(
      {},
      {
        get: () => () => {
          throw failure;
        },
      },
    ) as CreditStore;
    const credits = new CreditsService(throwingStore, null, null, { instrumentation });
    const ignoreFailure = async (callback: () => Promise<unknown>) => {
      await expect(callback()).rejects.toBeInstanceOf(Error);
    };

    await ignoreFailure(() =>
      credits.deduct(
        "private-user",
        { operation: "completion", measures: { calls: 1 } },
        { idempotencyKey: "private-key" },
      ),
    );
    await ignoreFailure(() =>
      credits.addCredits("private-user", new Decimal(1), { idempotencyKey: "private-key" }),
    );
    await ignoreFailure(() =>
      credits.executeGrantProgram({} as Parameters<CreditsService["executeGrantProgram"]>[0]),
    );
    await ignoreFailure(() =>
      credits.grantSubscriptionCycle("private-user", new Decimal(1), {
        idempotencyKey: "private-key",
      }),
    );
    await ignoreFailure(() =>
      credits.refundCredits("private-entry", { idempotencyKey: "private-key" }),
    );
    await ignoreFailure(() => credits.release("private-user", "private-lease"));
    await ignoreFailure(() =>
      credits.reserve("private-user", new Decimal(1), { idempotencyKey: "private-key" }),
    );
    await ignoreFailure(() => credits.settle("private-user", "private-lease", new Decimal(1)));

    const creditOperations = instrumentation.operations.map(({ operation }) => operation).sort();
    expect(creditOperations).toEqual(
      expectedOperations.filter((operation) => operation.startsWith("credits.")).sort(),
    );
    expect([...creditOperations, "postgres.query", "postgres.rpc"].sort()).toEqual(
      [...expectedOperations].sort(),
    );
    expect(JSON.stringify(instrumentation.operations)).not.toContain("private-user");
    expect(JSON.stringify(instrumentation.operations)).not.toContain("private-key");
  });
});
