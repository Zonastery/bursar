import { describe, it, expect, vi } from "vitest";
import type { PgPoolConstructor } from "../src/stores/postgres-store.js";
import { PostgresStore } from "../src/stores/postgres-store.js";

function makeMockPool(rows: unknown[]): PgPoolConstructor {
  return vi.fn(() => ({
    query: vi.fn().mockResolvedValue({ rows }),
    end: vi.fn().mockResolvedValue(undefined),
  })) as unknown as PgPoolConstructor;
}

describe("PostgresStore", () => {
  it("constructor stores database URL", () => {
    const store = new PostgresStore("postgresql://user:pass@localhost:5432/db", makeMockPool([]));
    expect(store).toBeInstanceOf(PostgresStore);
  });

  it("setup returns result", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.setup();
    expect(result.success).toBe(true);
  });

  it("getBalance returns zero for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.getBalance("user-1");
    expect(result.balance).toBe(0);
  });

  it("getBalance parses row result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ user_id: "user-1", balance: 100, lifetime_purchased: 200 }]),
    );
    const result = await store.getBalance("user-1");
    expect(result.balance).toBe(100);
    expect(result.lifetimePurchased).toBe(200);
  });

  it("addCredits returns default values for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.addCredits("user-1", 100);
    expect(result.transactionId).toBe("");
    expect(result.newBalance).toBe(0);
  });

  it("addCredits parses row result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        { id: "tx-1", user_id: "user-1", amount: 100, new_balance: 200, lifetime_purchased: 500 },
      ]),
    );
    const result = await store.addCredits("user-1", 100);
    expect(result.transactionId).toBe("tx-1");
    expect(result.newBalance).toBe(200);
  });

  it("reserveCredits returns no result error for empty", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.reserveCredits("user-1", 50, "usage");
    expect(result.error).toBe("no result");
  });

  it("reserveCredits parses row result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        { reservation_id: "res-1", user_id: "user-1", amount: 50, balance: 150, reserved: 50 },
      ]),
    );
    const result = await store.reserveCredits("user-1", 50, "usage");
    expect(result.reservationId).toBe("res-1");
    expect(result.amount).toBe(50);
  });

  it("reserveCredits maps error result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([{ error: "insufficient_credits" }]),
    );
    const result = await store.reserveCredits("user-1", 50, "usage");
    expect(result.error).toBe("insufficient_credits");
  });

  it("deductCredits returns error for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.deductCredits("user-1", "rid", 50);
    expect(result.error).toBe("no result");
  });

  it("deductCredits parses row result", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        { id: "tx-1", user_id: "user-1", amount: -50, new_balance: 50, idempotent: false },
      ]),
    );
    const result = await store.deductCredits("user-1", "rid", 50);
    expect(result.transactionId).toBe("tx-1");
    expect(result.amount).toBe(-50);
    expect(result.balanceAfter).toBe(50);
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
});
