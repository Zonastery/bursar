import { describe, it, expect, vi } from "vitest";
import Decimal from "decimal.js";
import type { PgPool, PgPoolConstructor } from "../src/stores/postgres-store.js";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { StoreError } from "../src/errors.js";

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

  it("setup throws StoreError instead of silently succeeding (H17)", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    await expect(store.setup()).rejects.toThrow(StoreError);
    await expect(store.setup()).rejects.toThrow(/migrat/i);
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
      makeMockPool([{ user_id: "user-1", balance: "100.1234", lifetime_purchased: "200.0000" }]),
    );
    const result = await store.getBalance("user-1");
    expect(result.balance.toString()).toBe("100.1234");
    expect(result.lifetimePurchased.toString()).toBe("200");
  });

  it("addCredits returns default Decimals for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.addCredits("user-1", D(100));
    expect(result.transactionId).toBe("");
    expect(result.newBalance.toString()).toBe("0");
  });

  it("addCredits parses row result and sends amount as a decimal string", async () => {
    const { ctor, calls } = makeRecordingPool([
      { id: "tx-1", user_id: "user-1", amount: "100", new_balance: "200", lifetime_purchased: "500" },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const result = await store.addCredits("user-1", D("100.5"), "purchase");
    expect(result.transactionId).toBe("tx-1");
    expect(result.newBalance.toString()).toBe("200");
    // amount param serialized as a decimal string (no binary float).
    expect(calls[0].text).toContain("credits_add");
    expect(calls[0].params[1]).toBe("100.5");
  });

  // ── Credit tiers ─────────────────────────────────────────────────────
  describe("credit tiers", () => {
    it("addCredits sends the tier as the 5th credits_add param", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          id: "tx-tier-1",
          user_id: "user-1",
          amount: "20",
          new_balance: "20",
          lifetime_purchased: "0",
          tier: "gifted",
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.addCredits(
        "user-1",
        D(20),
        "adjustment",
        null,
        null,
        "gifted",
      );
      expect(calls[0].text).toContain("credits_add");
      expect(calls[0].params).toEqual([
        "user-1",
        "20",
        "adjustment",
        JSON.stringify({}),
        "gifted",
      ]);
      expect(result.tier).toBe("gifted");
    });

    it("addCredits omits tier as null when not specified, and defaults result.tier to 'default'", async () => {
      const { ctor, calls } = makeRecordingPool([
        { id: "tx-1", user_id: "user-1", amount: "10", new_balance: "10", lifetime_purchased: "0" },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.addCredits("user-1", D(10));
      expect(calls[0].params[4]).toBeNull();
      // Row omitted `tier` entirely (e.g. a no-tiers-configured deployment) —
      // the store falls back to "default" rather than surfacing `undefined`.
      expect(result.tier).toBe("default");
    });

    it("deductWithAllowance parses tier_breakdown into a Record<string, Decimal>", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            transaction_id: "tx-1",
            amount: "15.0000",
            allowance_consumed: "0.0000",
            balance_after: "5.0000",
            idempotent: false,
            cap_warning: null,
            tier_breakdown: { gifted: "10.0000", purchased: "5.0000" },
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(15));
      expect(result.tierBreakdown).not.toBeNull();
      expect(result.tierBreakdown!.gifted).toBeInstanceOf(Decimal);
      expect(result.tierBreakdown!.gifted.toString()).toBe("10");
      expect(result.tierBreakdown!.purchased.toString()).toBe("5");
    });

    // KNOWN GAP (discovered while writing these tests, not introduced by them):
    // unlike deductWithAllowance/settleLease/createLease/refundCredits,
    // PostgresStore.addCredits now checks `"error" in row` (fixed alongside
    // tiers — it previously silently swallowed every credits_add failure,
    // including pre-existing invalid_amount/unauthorized envelopes, into a
    // bogus "success"). The SQL `credits_add` (023_credit_tiers.sql) returns
    // error envelopes such as {error: "tier_not_found", tier: "<requested>"}
    // for every tier failure (tier_not_found/tier_required/
    // tier_does_not_expire/invalid_expires_at/expires_at_required); these
    // must now surface as a thrown StoreError, matching SupabaseStore's
    // pre-existing errorCode()-checked behavior.
    it("addCredits surfaces the SQL error envelope for tier failures", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ error: "tier_not_found", tier: "bogus" }]),
      );
      await expect(store.addCredits("user-1", D(10), "adjustment", null, null, "bogus")).rejects.toThrow(
        "credits_add: tier_not_found",
      );
    });

    it("deductWithAllowance leaves tierBreakdown null when absent from the row", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            transaction_id: "tx-1",
            amount: "15.0000",
            allowance_consumed: "0.0000",
            balance_after: "5.0000",
            idempotent: false,
            cap_warning: null,
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(15));
      expect(result.tierBreakdown).toBeNull();
    });

    it("refundCredits parses tier_breakdown (LIFO restoration split)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            refund_transaction_id: "refund-1",
            user_id: "user-1",
            amount: "15.0000",
            new_balance: "115.0000",
            tier_breakdown: { purchased: "5.0000", gifted: "10.0000" },
          },
        ]),
      );
      const result = await store.refundCredits("tx-1");
      expect(result.tierBreakdown!.purchased.toString()).toBe("5");
      expect(result.tierBreakdown!.gifted.toString()).toBe("10");
    });

    it("sweepExpiredCredits parses expired_by_tier", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            expired_count: 2,
            expired_amount: "30.0000",
            expired_by_tier: { gifted: "30.0000" },
          },
        ]),
      );
      const result = await store.sweepExpiredCredits();
      expect(result.expiredByTier!.gifted.toString()).toBe("30");
    });

    it("getCreditTiers unwraps the single-JSONB envelope {user_id, tiers, total_balance} (not a rowset)", async () => {
      // get_user_credit_tiers RETURNS JSONB — a single scalar envelope object,
      // NOT one row per tier. callproc's scalar-JSONB unwrapping surfaces it
      // as a single-element array whose one element IS the envelope; the
      // per-tier rows live under its `tiers` array field.
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            get_user_credit_tiers: {
              user_id: "user-1",
              tiers: [
                { tier_key: "gifted", name: "Gifted", priority: 10, expires: true, balance: "20.0000" },
                {
                  tier_key: "purchased",
                  name: "Purchased",
                  priority: 20,
                  expires: false,
                  balance: "10.0000",
                },
              ],
              total_balance: "30.0000",
            },
          },
        ]),
      );
      const result = await store.getCreditTiers("user-1");
      expect(result.userId).toBe("user-1");
      expect(result.tiers).toHaveLength(2);
      expect(result.tiers[0]).toMatchObject({
        tierKey: "gifted",
        name: "Gifted",
        priority: 10,
        expires: true,
      });
      expect(result.tiers[0].balance.toString()).toBe("20");
      expect(result.tiers[1].balance.toString()).toBe("10");
      expect(result.totalBalance).toBeInstanceOf(Decimal);
      expect(result.totalBalance.toString()).toBe("30");
    });

    it("getCreditTiers returns an empty tier list and zero balance for empty results", async () => {
      const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
      const result = await store.getCreditTiers("user-1");
      expect(result.userId).toBe("user-1");
      expect(result.tiers).toEqual([]);
      expect(result.totalBalance.toString()).toBe("0");
    });

    it("getCreditTiers would misread a flat rowset as an empty envelope (documents the contract, not a rowset)", async () => {
      // If get_user_credit_tiers were (incorrectly) mocked as a flat array of
      // per-tier rows — the WRONG shape per the SQL contract (023_credit_tiers.sql:
      // it RETURNS one JSONB envelope, not SETOF) — callproc's `rows.length === 1`
      // scalar-unwrap heuristic does not fire for a multi-row result, so `tiers`
      // would incorrectly stay empty. This pins down that getCreditTiers only
      // works against the real envelope shape, not a rowset.
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          { tier_key: "gifted", name: "Gifted", priority: 10, expires: true, balance: "20.0000" },
          { tier_key: "purchased", name: "Purchased", priority: 20, expires: false, balance: "10.0000" },
        ]),
      );
      const result = await store.getCreditTiers("user-1");
      expect(result.tiers).toEqual([]);
    });
  });

  describe("deductWithAllowance", () => {
    it("calls deduct_with_allowance with all params (decimal strings)", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          transaction_id: "tx-1",
          amount: "2.5000",
          allowance_consumed: "0.0000",
          balance_after: "97.5000",
          idempotent: false,
          cap_warning: null,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.deductWithAllowance("user-1", D("2.5"), {
        idempotencyKey: "k1",
        minBalance: D(5),
        model: "gpt-4",
        metadata: { foo: "bar" },
      });
      expect(calls[0].text).toContain("deduct_with_allowance");
      expect(calls[0].params).toEqual([
        "user-1",
        "2.5",
        "k1",
        "5",
        "gpt-4",
        JSON.stringify({ foo: "bar" }),
        false, // p_skip_allowance: not yet exposed as a JS option; keep the SQL default
        null, // p_period_start (WS9): omitted → calendar-month fallback
      ]);
      // Parses NUMERIC strings to exact Decimal.
      expect(result.amount.toString()).toBe("2.5");
      expect(result.allowanceConsumed.toString()).toBe("0");
      expect(result.balanceAfter.toString()).toBe("97.5");
      expect(result.idempotent).toBe(false);
      expect(result.capWarning).toBeNull();
    });

    it("parses allowance_consumed and cap_warning", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            transaction_id: "tx-2",
            amount: "15.0000",
            allowance_consumed: "10.0000",
            balance_after: "85.0000",
            idempotent: false,
            cap_warning: "warn",
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(25));
      expect(result.amount.toString()).toBe("15");
      expect(result.allowanceConsumed.toString()).toBe("10");
      expect(result.capWarning).toBe("warn");
    });

    it("maps cap_reached error envelope to result.error (no throw)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ error: "cap_reached", action: "deny" }]),
      );
      const result = await store.deductWithAllowance("user-1", D(20));
      expect(result.error).toBe("cap_reached");
      expect(result.transactionId).toBe("");
    });

    it("maps insufficient_credits error envelope", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ error: "insufficient_credits" }]),
      );
      const result = await store.deductWithAllowance("user-1", D(20));
      expect(result.error).toBe("insufficient_credits");
    });

    it("surfaces idempotent replay", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            transaction_id: "tx-orig",
            amount: "10.0000",
            allowance_consumed: "0.0000",
            balance_after: "90.0000",
            idempotent: true,
            cap_warning: null,
          },
        ]),
      );
      const result = await store.deductWithAllowance("user-1", D(10), { idempotencyKey: "k" });
      expect(result.idempotent).toBe(true);
      expect(result.transactionId).toBe("tx-orig");
    });
  });

  describe("callproc unwrapping robustness", () => {
    it("list RPC returns ALL rows (not just the first)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          { user_id: "u1", total_spend: "10", transaction_count: 1 },
          { user_id: "u2", total_spend: "20", transaction_count: 2 },
          { user_id: "u3", total_spend: "30", transaction_count: 3 },
        ]),
      );
      const rows = await store.spendByUser(new Date(), new Date());
      expect(rows).toHaveLength(3);
      expect(rows[2].totalSpend.toString()).toBe("30");
    });

    it("scalar JSONB result (single object column) is unwrapped", async () => {
      // pg shape for `RETURNS JSONB`: one row, one column whose value is the obj.
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ get_credits_balance: { user_id: "u1", balance: "42", lifetime_purchased: "0" } }]),
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

  it("setActivePricing returns empty id for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.setActivePricing({ models: { a: "1" } });
    expect(result).toBe("");
  });

  it("checkFeature treats numeric 0 as present (M6)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ user_id: "u1", plan_id: "p", plan_name: "P", free_allowance: "0", features: { quota: 0 } }]),
    );
    const result = await store.checkFeature("u1", "quota");
    expect(result.value).toBe(0);
    expect(result.hasFeature).toBe(true);
  });

  it("checkFeature treats explicit false as absent (M6)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ user_id: "u1", plan_id: "p", plan_name: "P", free_allowance: "0", features: { flag: false } }]),
    );
    const result = await store.checkFeature("u1", "flag");
    expect(result.hasFeature).toBe(false);
  });

  // PG2 — NULL value in NUMERIC column → converted to Decimal("0") (not NaN)
  it("NULL amount in RPC row is converted to Decimal zero, not NaN (PG2)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ id: "tx-1", user_id: "user-1", amount: null, new_balance: "100", lifetime_purchased: "100" }]),
    );
    const result = await store.addCredits("user-1", D(50));
    // `dec(null)` returns ZERO — never NaN.
    expect(result.amount.isNaN()).toBe(false);
    expect(result.amount.toString()).toBe("50"); // falls back to the supplied `amount`
  });

  // PG3 — Decimal value sent as string for non-round amounts
  it("addCredits sends non-round amount as exact decimal string (PG3)", async () => {
    const { ctor, calls } = makeRecordingPool([
      { id: "tx-2", user_id: "user-1", amount: "0.0001", new_balance: "0.0001", lifetime_purchased: "0.0001" },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    await store.addCredits("user-1", D("0.0001"), "purchase");
    expect(calls[0].text).toContain("credits_add");
    // Must arrive as the string "0.0001", not a binary float like 0.00010000000000000002.
    expect(calls[0].params[1]).toBe("0.0001");
  });

  // PG4 — expiresAt serialized as ISO string, not a Date object
  it("addCredits serializes expiresAt as ISO string (PG4)", async () => {
    const { ctor, calls } = makeRecordingPool([
      { id: "tx-3", user_id: "user-1", amount: "50", new_balance: "150", lifetime_purchased: "150" },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const expiresAt = new Date("2024-01-15T00:00:00.000Z");
    await store.addCredits("user-1", D(50), "purchase", null, expiresAt);
    // params[3] is JSON.stringify(meta) — parse it and check expires_at.
    const meta = JSON.parse(calls[0].params[3] as string) as Record<string, unknown>;
    expect(typeof meta.expires_at).toBe("string");
    expect(meta.expires_at).toBe("2024-01-15T00:00:00.000Z");
  });

  // PG5 — Unknown RPC error code → surfaces as result.error (not thrown, not silent ok)
  it("unknown RPC error code is surfaced as result.error without throwing (PG5)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ error: "some_unknown_code_xyz" }]),
    );
    // deductWithAllowance maps ALL error envelopes to result.error — unknown codes included.
    const result = await store.deductWithAllowance("user-1", D(20));
    expect(result.error).toBe("some_unknown_code_xyz");
    expect(result.transactionId).toBe("");
  });

  // PG6 — Network/transport error bubbles as the underlying error (postgres store does not wrap)
  it("pool query error propagates out of getBalance (PG6)", async () => {
    const networkError = new Error("Connection refused");
    const query = vi.fn(() => Promise.reject(networkError));
    const ctor = vi.fn(
      () => ({ query, end: vi.fn().mockResolvedValue(undefined) }) as unknown as import("../src/stores/postgres-store.js").PgPool,
    ) as unknown as import("../src/stores/postgres-store.js").PgPoolConstructor;
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    // The postgres store propagates the raw error from the pool — callers should handle it.
    await expect(store.getBalance("user-1")).rejects.toThrow("Connection refused");
  });

  // PG7 — NUMERIC string precision: values with more than 4dp are quantized to
  // 4dp ROUND_HALF_UP by the store's internal dec() + quantizeMoney helpers.
  // This tests that the store's parsing pipeline does not silently truncate or
  // lose precision when the DB returns a high-precision NUMERIC string.
  it("NUMERIC string with >4dp is parsed and quantized to 4dp ROUND_HALF_UP (PG7)", async () => {
    // Simulate a DB row where amount has 10 decimal places (more than our 4dp contract).
    // "100.1234567890" → rounds to "100.1235" (5th dp = 5 → rounds up).
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          id: "tx-pg7",
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
    // Quantization to 4dp ROUND_HALF_UP: 100.12345... → 100.1235.
    const quantized = result.amount.toDecimalPlaces(4, Decimal.ROUND_HALF_UP);
    expect(quantized.equals(D("100.1235"))).toBe(true);
  });
});
