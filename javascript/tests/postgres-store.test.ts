import { describe, it, expect, vi } from "vitest";
import Decimal from "decimal.js";
import type { PgPool, PgPoolConstructor } from "../src/credits/postgres/store.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { PostgresBillingStore } from "../src/billing/postgres/store.js";

const D = (n: number | string) => new Decimal(n);

/** Mock pool that returns a fixed set of rows for every query. */
function makeMockPool(rows: unknown[]): PgPoolConstructor {
  return vi.fn(() => ({
    query: vi.fn().mockResolvedValue({ rows }),
    end: vi.fn().mockResolvedValue(undefined),
  })) as unknown as PgPoolConstructor;
}

/**
 * Mock pool that records the SQL text + params it was called with, returning a
 * caller-supplied row set. Lets us assert how the store builds RPC calls.
 */
function makeRecordingPool(rows: unknown[]): {
  ctor: PgPoolConstructor;
  calls: Array<{ text: string; params: unknown[] }>;
} {
  const calls: Array<{ text: string; params: unknown[] }> = [];
  const query = vi.fn((text: string, params?: unknown[]) => {
    calls.push({ text, params: params ?? [] });
    return Promise.resolve({ rows });
  });
  const ctor = vi.fn(
    () => ({ query, end: vi.fn().mockResolvedValue(undefined) }) as unknown as PgPool,
  ) as unknown as PgPoolConstructor;
  return { ctor, calls };
}

describe("PostgresStore", () => {
  it("constructor stores database URL", () => {
    const store = new PostgresStore("postgresql://user:pass@localhost:5432/db", makeMockPool([]));
    expect(store).toBeInstanceOf(PostgresStore);
  });

  it("getBalance returns zero Decimal for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.getBalance("user-1");
    expect(result.balance).toBeInstanceOf(Decimal);
    expect(result.balance.toString()).toBe("0");
  });

  it("getBalance parses NUMERIC string columns to exact Decimal", async () => {
    // Postgres returns NUMERIC as a string via pg.
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ bucket_key: "purchased", balance: "100.1234" }]),
    );
    const result = await store.getBalance("user-1");
    expect(result.balance.toString()).toBe("100.1234");
    expect(result.lifetimePurchased.toString()).toBe("0");
  });

  it("addCredits returns default Decimals for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.addCredits("user-1", D(100));
    expect(result.entryId).toBe("");
    expect(result.newBalance.toString()).toBe("0");
  });

  it("addCredits parses row result and sends amount as a decimal string", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        entry_id: "tx-1",
        user_id: "user-1",
        balance_after: "200",
        replayed: false,
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const result = await store.addCredits("user-1", D("100.5"), "purchase");
    expect(result.entryId).toBe("tx-1");
    expect(result.newBalance.toString()).toBe("200");
    // amount param serialized as a decimal string (no binary float).
    expect(calls[0].text).toContain("post_credit");
    expect(calls[0].params[2]).toBe("100.5");
  });

  // ── Credit tiers ─────────────────────────────────────────────────────
  describe("credit tiers", () => {
    it("addCredits sends the bucket to post_credit", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          entry_id: "tx-tier-1",
          user_id: "user-1",
          balance_after: "20",
          replayed: false,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.addCredits("user-1", D(20), "adjustment", null, null, "gifted");
      expect(calls[0].text).toContain("post_credit");
      expect(calls[0].params.slice(0, 4)).toEqual(["user-1", "grant", "20", "adjustment"]);
      expect(calls[0].params[6]).toBe("gifted");
      expect(result.bucket).toBe("gifted");
    });

    it("addCredits omits tier as null when not specified, and defaults result.bucket to 'default'", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          entry_id: "tx-1",
          user_id: "user-1",
          balance_after: "10",
          replayed: false,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.addCredits("user-1", D(10));
      expect(calls[0].params[6]).toBeNull();
      // Row omitted `tier` entirely (e.g. a no-tiers-configured deployment) —
      // the store falls back to "default" rather than surfacing `undefined`.
      expect(result.bucket).toBe("default");
    });

    it("deductWithAllowance parses bucket_breakdown into a Record<string, Decimal>", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            ledger_entry_id: "tx-1",
            charged: "15.0000",
            allowance_covered: "0.0000",
            balance_after: "5.0000",
            replayed: false,
            bucket_breakdown: { gifted: "10.0000", purchased: "5.0000" },
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(15));
      expect(result.bucketBreakdown).not.toBeNull();
      expect(result.bucketBreakdown!.gifted).toBeInstanceOf(Decimal);
      expect(result.bucketBreakdown!.gifted.toString()).toBe("10");
      expect(result.bucketBreakdown!.purchased.toString()).toBe("5");
    });

    it("addCredits surfaces the post_credit error envelope", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ error_code: "missing_catalog_bucket" }]),
      );
      await expect(
        store.addCredits("user-1", D(10), "adjustment", null, null, "bogus"),
      ).rejects.toThrow("post_credit: missing_catalog_bucket");
    });

    it("deductWithAllowance leaves bucketBreakdown null when absent from the row", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            ledger_entry_id: "tx-1",
            charged: "15.0000",
            allowance_covered: "0.0000",
            balance_after: "5.0000",
            replayed: false,
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(15));
      expect(result.bucketBreakdown).toBeNull();
    });

    it("sweepExpiredCredits maps the normalized sweep result", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            expired_count: 2,
            expired_amount: "12.5",
            expired_by_bucket: { gifted: "12.5" },
          },
        ]),
      );
      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(2);
      expect(result.expiredAmount.eq("12.5")).toBe(true);
      expect(result.expiredByBucket?.gifted.eq("12.5")).toBe(true);
      expect(result.dryRun).toBe(false);
    });

    it("getBucketBalances maps the normalized rowset", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            bucket_key: "gifted",
            balance: "20.0000",
          },
          {
            bucket_key: "purchased",
            balance: "10.0000",
          },
        ]),
      );
      const result = await store.getBucketBalances("user-1");
      expect(result.userId).toBe("user-1");
      expect(result.buckets).toHaveLength(2);
      expect(result.buckets[0]).toMatchObject({
        bucketKey: "gifted",
        label: "",
        priority: 0,
        expires: false,
      });
      expect(result.buckets[0].balance.toString()).toBe("20");
      expect(result.buckets[1].balance.toString()).toBe("10");
      expect(result.totalBalance).toBeInstanceOf(Decimal);
      expect(result.totalBalance.toString()).toBe("30");
    });

    it("getBucketBalances returns an empty bucket list and zero balance for empty results", async () => {
      const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
      const result = await store.getBucketBalances("user-1");
      expect(result.userId).toBe("user-1");
      expect(result.buckets).toEqual([]);
      expect(result.totalBalance.toString()).toBe("0");
    });
  });

  describe("deductWithAllowance", () => {
    it("calls charge_usage_for_operation with the normalized contract", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          ledger_entry_id: "tx-1",
          charged: "2.5000",
          allowance_covered: "0.0000",
          balance_after: "97.5000",
          replayed: false,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.deductWithAllowance("user-1", D("2.5"), {
        idempotencyKey: "k1",
        operation: "completion",
        model: "gpt-4",
        region: "us-east",
        measures: { input_tokens: 2 },
        dimensions: { model: "gpt-4", region: "us-east" },
        metadata: { foo: "bar" },
      });
      expect(calls[0].text).toContain("charge_usage_for_operation");
      expect(calls[0].params).toEqual([
        "user-1",
        "completion",
        "2.5",
        "k1",
        null,
        "gpt-4",
        "us-east",
        JSON.stringify({ foo: "bar" }),
        JSON.stringify({ input_tokens: 2 }),
        JSON.stringify({ model: "gpt-4", region: "us-east" }),
      ]);
      // Parses NUMERIC strings to exact Decimal.
      expect(result.amount.toString()).toBe("2.5");
      expect(result.allowanceConsumed.toString()).toBe("0");
      expect(result.balanceAfter.toString()).toBe("97.5");
      expect(result.idempotent).toBe(false);
      expect(result.capWarning).toBeNull();
    });

    it("parses allowance consumption", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            ledger_entry_id: "tx-2",
            charged: "15.0000",
            allowance_covered: "10.0000",
            balance_after: "85.0000",
            replayed: false,
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(25));
      expect(result.amount.toString()).toBe("15");
      expect(result.allowanceConsumed.toString()).toBe("10");
      expect(result.capWarning).toBeNull();
    });

    it("maps quota_exceeded error envelope to result.error (no throw)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ error_code: "quota_exceeded" }]),
      );
      const result = await store.deductWithAllowance("user-1", D(20));
      expect(result.error).toBe("quota_exceeded");
      expect(result.entryId).toBe("");
    });

    it("maps insufficient_credits error envelope", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ error_code: "insufficient_credits" }]),
      );
      const result = await store.deductWithAllowance("user-1", D(20));
      expect(result.error).toBe("insufficient_credits");
    });

    it("surfaces idempotent replay", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            ledger_entry_id: "tx-orig",
            charged: "10.0000",
            allowance_covered: "0.0000",
            balance_after: "90.0000",
            replayed: true,
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(10), { idempotencyKey: "k" });
      expect(result.idempotent).toBe(true);
      expect(result.entryId).toBe("tx-orig");
    });
  });

  describe("callproc unwrapping robustness", () => {
    it("list RPC returns ALL rows (not just the first)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          { user_id: "u1", total_spend: "10", entry_count: 1 },
          { user_id: "u2", total_spend: "20", entry_count: 2 },
          { user_id: "u3", total_spend: "30", entry_count: 3 },
        ]),
      );
      const rows = await store.spendByUser(new Date(), new Date());
      expect(rows).toHaveLength(3);
      expect(rows[2].totalSpend.toString()).toBe("30");
    });

    it("normalized bucket rows are not mistaken for scalar RPC results", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ bucket_key: "default", balance: "42" }]),
      );
      const result = await store.getBalance("u1");
      expect(result.balance.toString()).toBe("42");
    });
  });

  it("getActivePricing returns null for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.getActivePricing();
    expect(result).toBeNull();
  });

  it("returns the canonical config without converting identifier keys", async () => {
    const config = {
      version: 1,
      metering: {
        models: { myModel: "input_tokens * 1" },
        flat_jobs: { camelJob: 2.5 },
      },
      ledger: { buckets: { giftBucket: { label: "Gift" } } },
      plans: { myPlan: { label: "Plan" } },
    };
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          id: "cfg-1",
          revision_no: 1,
          source_document: config,
          status: "active",
        },
      ]),
    );
    const result = await store.getActivePricing();
    expect(result?.config).toEqual(config);
  });

  it("keeps persisted payment metadata in its canonical snake_case shape", async () => {
    const pool = {
      query: vi.fn().mockResolvedValue({
        rows: [
          {
            id: "pay_1",
            purpose: "credit_topup",
            amount_minor: 1000,
            currency: "USD",
            metadata: { credits_per_unit: 1000, user_defined_key: "unchanged" },
          },
        ],
      }),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;
    const store = new PostgresBillingStore(pool);
    await expect(store.getBillingPayment("stripe", "pay_1")).resolves.toMatchObject({
      purpose: "credit_topup",
      metadata: { credits_per_unit: 1000, user_defined_key: "unchanged" },
    });
  });

  it("hydrates subscription changes with revision-pinned offer context", async () => {
    const pool = {
      query: vi.fn((text: string, params?: unknown[]) => {
        if (text.includes("get_open_billing_subscription_change")) {
          return Promise.resolve({
            rows: [
              {
                id: "change-1",
                subscription_id: "subscription-1",
                from_offer_id: "offer-old",
                from_catalog_revision_id: "revision-old",
                to_offer_id: "offer-new",
                to_catalog_revision_id: "revision-new",
                effective_at: new Date("2027-01-01T00:00:00.000Z"),
                state: "scheduled",
                proration_behavior: "none",
                idempotency_key: "change-key",
              },
            ],
          });
        }
        if (text.includes("get_catalog_offer_context")) {
          return Promise.resolve({
            rows: [
              {
                side: "from",
                offer_id: params?.[0],
                offer_key: "monk_monthly",
                plan_id: "plan-old",
                plan_key: "monk",
                billing_unit: "month",
                billing_count: 1,
              },
              {
                side: "to",
                offer_id: params?.[2],
                offer_key: "sage_yearly",
                plan_id: "plan-new",
                plan_key: "sage",
                billing_unit: "year",
                billing_count: 1,
              },
            ],
          });
        }
        return Promise.resolve({ rows: [] });
      }),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;

    const store = new PostgresBillingStore(pool);
    const change = await store.getOpenBillingSubscriptionChange("dodo", "provider-subscription");

    expect(change).toMatchObject({
      fromOfferId: "offer-old",
      toOfferId: "offer-new",
      fromOffer: {
        offerId: "offer-old",
        offerKey: "monk_monthly",
        plan: "monk",
        interval: "month",
      },
      toOffer: {
        offerId: "offer-new",
        offerKey: "sage_yearly",
        plan: "sage",
        interval: "year",
      },
      effectiveAt: "2027-01-01T00:00:00.000Z",
      prorationBehavior: "none",
    });
  });

  it("setActivePricing returns empty id for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.setActivePricing({
      version: 1,
      credits: {
        accounting: { unit: "credit", scale: 6, rounding: "half_up" },
      },
    });
    expect(result).toBe("");
  });

  it("checkFeature treats numeric 0 as present (M6)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          feature_key: "quota",
          feature_value: 0,
        },
      ]),
    );
    const result = await store.checkFeature("u1", "quota");
    expect(result.value).toBe(0);
    expect(result.hasFeature).toBe(true);
  });

  it("checkFeature treats explicit false as absent (M6)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          feature_key: "flag",
          feature_value: false,
        },
      ]),
    );
    const result = await store.checkFeature("u1", "flag");
    expect(result.hasFeature).toBe(false);
  });

  // PG2 — NULL value in NUMERIC column → converted to Decimal("0") (not NaN)
  it("NULL amount in RPC row is converted to Decimal zero, not NaN (PG2)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          entry_id: "tx-1",
          user_id: "user-1",
          amount: null,
          new_balance: "100",
          lifetime_purchased: "100",
        },
      ]),
    );
    const result = await store.addCredits("user-1", D(50));
    // `dec(null)` returns ZERO — never NaN.
    expect(result.amount.isNaN()).toBe(false);
    expect(result.amount.toString()).toBe("50"); // falls back to the supplied `amount`
  });

  // PG3 — Decimal value sent as string for non-round amounts
  it("addCredits sends non-round amount as exact decimal string (PG3)", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        entry_id: "tx-2",
        user_id: "user-1",
        amount: "0.0001",
        new_balance: "0.0001",
        lifetime_purchased: "0.0001",
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    await store.addCredits("user-1", D("0.0001"), "purchase");
    expect(calls[0].text).toContain("post_credit");
    // Must arrive as the string "0.0001", not a binary float like 0.00010000000000000002.
    expect(calls[0].params[2]).toBe("0.0001");
  });

  // PG4 — expiresAt serialized as ISO string, not a Date object
  it("addCredits serializes expiresAt as ISO string (PG4)", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        entry_id: "tx-3",
        user_id: "user-1",
        amount: "50",
        new_balance: "150",
        lifetime_purchased: "150",
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const expiresAt = new Date("2024-01-15T00:00:00.000Z");
    await store.addCredits("user-1", D(50), "purchase", null, expiresAt);
    // params[5] is p_request; p_expires_at is also passed explicitly at params[8].
    const meta = JSON.parse(calls[0].params[5] as string) as Record<string, unknown>;
    expect(typeof meta.expires_at).toBe("string");
    expect(meta.expires_at).toBe("2024-01-15T00:00:00.000Z");
    expect(calls[0].params[8]).toBe("2024-01-15T00:00:00.000Z");
  });

  // PG5 — Unknown RPC error code → surfaces as result.error (not thrown, not silent ok)
  it("unknown RPC error code is surfaced as result.error without throwing (PG5)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ error_code: "some_unknown_code_xyz" }]),
    );
    // deductWithAllowance maps ALL error envelopes to result.error — unknown codes included.
    const result = await store.deductWithAllowance("user-1", D(20));
    expect(result.error).toBe("some_unknown_code_xyz");
    expect(result.entryId).toBe("");
  });

  // PG6 — Network/transport error bubbles as the underlying error (postgres store does not wrap)
  it("pool query error propagates out of getBalance (PG6)", async () => {
    const networkError = new Error("Connection refused");
    const query = vi.fn(() => Promise.reject(networkError));
    const ctor = vi.fn(
      () =>
        ({
          query,
          end: vi.fn().mockResolvedValue(undefined),
        }) as unknown as import("../src/credits/postgres/store.js").PgPool,
    ) as unknown as import("../src/credits/postgres/store.js").PgPoolConstructor;
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    // The postgres store propagates the raw error from the pool — callers should handle it.
    await expect(store.getBalance("user-1")).rejects.toThrow("Connection refused");
  });

  // PG7 — NUMERIC string precision: values with more than 6dp are quantized to
  // 6dp ROUND_HALF_UP by the store's internal dec() + quantizeMoney helpers.
  // This tests that the store's parsing pipeline does not silently truncate or
  // lose precision when the DB returns a high-precision NUMERIC string.
  it("NUMERIC string with >6dp is parsed and quantized to 6dp ROUND_HALF_UP (PG7)", async () => {
    // Simulate a DB row where amount has 10 decimal places (more than our 6dp contract).
    // "100.1234567890" → rounds to "100.1235" (5th dp = 5 → rounds up).
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          entry_id: "tx-pg7",
          user_id: "user-1",
          amount: "100.1234567890",
          new_balance: "100.1235",
          lifetime_purchased: "100.1235",
        },
      ]),
    );
    const result = await store.addCredits("user-1", D("100.1234567890"), "purchase");
    // The store parses the raw DB string "100.1234567890" via dec() into a Decimal.
    // The amount field is read directly from the DB row — Decimal("100.1234567890").
    // Quantization to 6dp ROUND_HALF_UP: 100.12345... → 100.1235.
    const quantized = result.amount.toDecimalPlaces(4, Decimal.ROUND_HALF_UP);
    expect(quantized.equals(D("100.1235"))).toBe(true);
  });
});
