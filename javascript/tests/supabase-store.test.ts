import { describe, it, expect, vi, afterEach } from "vitest";
import Decimal from "decimal.js";
import { HttpxSupabaseStore } from "../src/stores/supabase-store.js";
import { StoreError } from "../src/errors.js";

const D = (n: number | string) => new Decimal(n);
const URL_BASE = "https://test.supabase.co";
const KEY = "service-role-key";

interface CapturedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

/**
 * Install a mock global `fetch` that records the request and returns the given
 * JSON body with the given status. Returns the captured-requests array.
 */
function mockFetch(body: unknown, status = 200): CapturedRequest[] {
  const captured: CapturedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      captured.push({
        url,
        method: init.method ?? "GET",
        headers: init.headers as Record<string, string>,
        body: init.body ? JSON.parse(init.body as string) : undefined,
      });
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
      } as Response);
    }),
  );
  return captured;
}

/** Install a mock fetch that returns a non-JSON / invalid-JSON body. */
function mockFetchInvalidJson(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.reject(new SyntaxError("Unexpected token < in JSON")),
        text: () => Promise.resolve("<html>not json</html>"),
      } as unknown as Response),
    ),
  );
}

/** Install a mock fetch that rejects at the transport layer (network error). */
function mockFetchNetworkError(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("fetch failed: ECONNREFUSED"))),
  );
}

describe("HttpxSupabaseStore", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("construction", () => {
    it("stores url and key", () => {
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      expect(store).toBeInstanceOf(HttpxSupabaseStore);
    });

    it("strips trailing slashes from url", async () => {
      const captured = mockFetch({ user_id: "u1", balance: "0", lifetime_purchased: "0" });
      const store = new HttpxSupabaseStore("https://test.supabase.co///", KEY);
      await store.getBalance("u1");
      expect(captured[0].url).toBe("https://test.supabase.co/rest/v1/rpc/get_credits_balance");
    });
  });

  describe("setup", () => {
    it("throws StoreError (cannot run migrations over REST)", async () => {
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.setup()).rejects.toThrow(StoreError);
      await expect(store.setup()).rejects.toThrow(/migrat/i);
    });
  });

  describe("request contract (URL / headers / body)", () => {
    it("getBalance posts to the right URL with auth headers", async () => {
      const captured = mockFetch({ user_id: "u1", balance: "100.5", lifetime_purchased: "200" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.getBalance("u1");

      expect(captured).toHaveLength(1);
      const req = captured[0];
      expect(req.url).toBe(`${URL_BASE}/rest/v1/rpc/get_credits_balance`);
      expect(req.method).toBe("POST");
      expect(req.headers.apikey).toBe(KEY);
      expect(req.headers.authorization).toBe(`Bearer ${KEY}`);
      expect(req.headers["content-type"]).toBe("application/json");
      expect(req.body).toEqual({ p_user_id: "u1" });

      // NUMERIC string parsed to exact Decimal.
      expect(result.balance.toString()).toBe("100.5");
    });

    it("addCredits sends amount as decimal string in body", async () => {
      const captured = mockFetch({
        id: "tx-1",
        user_id: "u1",
        amount: "100.25",
        new_balance: "100.25",
        lifetime_purchased: "100.25",
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.addCredits("u1", D("100.25"), "purchase");

      expect(captured[0].url).toBe(`${URL_BASE}/rest/v1/rpc/credits_add`);
      expect(captured[0].body).toEqual({
        p_user_id: "u1",
        p_amount: "100.25",
        p_type: "purchase",
        p_metadata: {},
      });
      expect(result.newBalance.toString()).toBe("100.25");
    });

    it("deductWithAllowance sends full param shape", async () => {
      const captured = mockFetch({
        transaction_id: "tx-1",
        amount: "2.5",
        allowance_consumed: "0",
        balance_after: "97.5",
        idempotent: false,
        cap_warning: null,
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.deductWithAllowance("u1", D("2.5"), {
        idempotencyKey: "k1",
        minBalance: D(5),
        model: "gpt-4",
        metadata: { tier: "pro" },
      });

      expect(captured[0].url).toBe(`${URL_BASE}/rest/v1/rpc/deduct_with_allowance`);
      expect(captured[0].body).toEqual({
        p_user_id: "u1",
        p_amount: "2.5",
        p_idempotency_key: "k1",
        p_min_balance: "5",
        p_model: "gpt-4",
        p_metadata: { tier: "pro" },
      });
      expect(result.amount.toString()).toBe("2.5");
      expect(result.balanceAfter.toString()).toBe("97.5");
    });

    it("deductTeam threads the idempotency key into metadata (H12)", async () => {
      const captured = mockFetch({
        transaction_id: "tx-1",
        team_id: "team-1",
        user_id: "u1",
        amount: "-50",
        team_balance_after: "450",
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await store.deductTeam("team-1", "u1", D(50), { model: "gpt-4" }, "team-idem-1");

      expect(captured[0].url).toBe(`${URL_BASE}/rest/v1/rpc/deduct_team`);
      expect(captured[0].body).toEqual({
        p_team_id: "team-1",
        p_user_id: "u1",
        p_amount: "50",
        p_metadata: { model: "gpt-4", idempotency_key: "team-idem-1" },
      });
    });
  });

  describe("error-envelope handling (business errors → result.error)", () => {
    it("deductWithAllowance maps cap_reached envelope to result.error (no throw)", async () => {
      mockFetch({ error: "cap_reached", action: "deny" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.deductWithAllowance("u1", D(20));
      expect(result.error).toBe("cap_reached");
      expect(result.transactionId).toBe("");
    });

    it("deductWithAllowance maps insufficient_credits envelope", async () => {
      mockFetch({ error: "insufficient_credits" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.deductWithAllowance("u1", D(20));
      expect(result.error).toBe("insufficient_credits");
    });

    it("reserveCredits maps error envelope", async () => {
      mockFetch({ error: "insufficient_credits", balance: "10", reserved: "0" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.reserveCredits("u1", D(50), "usage");
      expect(result.error).toBe("insufficient_credits");
      expect(result.balance.toString()).toBe("10");
    });

    it("refundCredits maps over_refund envelope", async () => {
      mockFetch({ error: "over_refund", user_id: "u1", new_balance: "100" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.refundCredits("tx-1", D(1000));
      expect(result.error).toBe("over_refund");
      expect(result.newBalance.toString()).toBe("100");
    });

    it("deductTeam maps insufficient_team_balance envelope", async () => {
      mockFetch({ error: "insufficient_team_balance", team_balance_after: "10" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.deductTeam("team-1", "u1", D(100));
      expect(result.error).toBe("insufficient_team_balance");
    });

    it("getBalance throws StoreError on an unexpected error envelope", async () => {
      mockFetch({ error: "unauthorized" });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.getBalance("u1")).rejects.toThrow(StoreError);
    });
  });

  describe("transport / JSON errors → StoreError", () => {
    it("wraps a non-2xx HTTP response in StoreError", async () => {
      mockFetch("internal error", 500);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.getBalance("u1")).rejects.toThrow(StoreError);
      await expect(store.getBalance("u1")).rejects.toThrow(/500/);
    });

    it("wraps a network/transport failure in StoreError", async () => {
      mockFetchNetworkError();
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.getBalance("u1")).rejects.toThrow(StoreError);
      await expect(store.addCredits("u1", D(10))).rejects.toThrow(StoreError);
    });

    it("wraps an invalid-JSON response body in StoreError", async () => {
      mockFetchInvalidJson();
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.getBalance("u1")).rejects.toThrow(StoreError);
      await expect(store.getBalance("u1")).rejects.toThrow(/invalid JSON/i);
    });
  });

  describe("set-returning RPCs return all rows", () => {
    it("spendByUser parses every row as Decimal", async () => {
      mockFetch([
        { user_id: "u1", total_spend: "10.5", transaction_count: 1 },
        { user_id: "u2", total_spend: "20", transaction_count: 2 },
      ]);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const rows = await store.spendByUser(new Date(), new Date());
      expect(rows).toHaveLength(2);
      expect(rows[0].totalSpend.toString()).toBe("10.5");
      expect(rows[1].totalSpend.toString()).toBe("20");
    });
  });

  describe("checkFeature presence vs truthiness (M6)", () => {
    it("treats numeric 0 as present", async () => {
      mockFetch({ user_id: "u1", plan_id: "p", plan_name: "P", free_allowance: "0", features: { quota: 0 } });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.checkFeature("u1", "quota");
      expect(result.value).toBe(0);
      expect(result.hasFeature).toBe(true);
    });

    it("treats explicit false as absent", async () => {
      mockFetch({ user_id: "u1", plan_id: "p", plan_name: "P", free_allowance: "0", features: { flag: false } });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.checkFeature("u1", "flag");
      expect(result.hasFeature).toBe(false);
    });
  });
});
