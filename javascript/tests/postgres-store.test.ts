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

  it("reserveCredits returns no result error for empty", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.reserveCredits("user-1", D(50), "usage");
    expect(result.error).toBe("no result");
  });

  it("reserveCredits parses row result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        { reservation_id: "res-1", user_id: "user-1", amount: "50", balance: "150", reserved: "50" },
      ]),
    );
    const result = await store.reserveCredits("user-1", D(50), "usage");
    expect(result.reservationId).toBe("res-1");
    expect(result.amount.toString()).toBe("50");
  });

  it("reserveCredits maps error envelope", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ error: "insufficient_credits" }]),
    );
    const result = await store.reserveCredits("user-1", D(50), "usage");
    expect(result.error).toBe("insufficient_credits");
  });

  it("deductCredits returns error for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.deductCredits("user-1", "rid", D(50));
    expect(result.error).toBe("no result");
  });

  it("deductCredits parses row result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        { id: "tx-1", user_id: "user-1", amount: "-50", new_balance: "50", idempotent: false },
      ]),
    );
    const result = await store.deductCredits("user-1", "rid", D(50));
    expect(result.transactionId).toBe("tx-1");
    expect(result.amount.toString()).toBe("-50");
    expect(result.balanceAfter.toString()).toBe("50");
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
});
