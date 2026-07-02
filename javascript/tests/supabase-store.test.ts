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
        p_tier: null,
        p_idempotency_key: null,
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
        p_period_start: null, // WS9: omitted → calendar-month fallback
        p_feature: null,
        p_feature_max_calls: null,
        p_feature_action: null,
        p_feature_period_start: null,
        p_feature_period_end: null,
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

    it("addCredits threads a non-null idempotencyKey and tier into the credits_add RPC body", async () => {
      const captured = mockFetch({
        id: "tx-2",
        user_id: "u1",
        amount: "50",
        new_balance: "150",
        lifetime_purchased: "150",
        tier: "gifted",
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.addCredits(
        "u1",
        D(50),
        "purchase",
        undefined,
        undefined,
        "gifted",
        "evt-1",
      );

      expect(captured[0].url).toBe(`${URL_BASE}/rest/v1/rpc/credits_add`);
      expect(captured[0].body).toEqual({
        p_user_id: "u1",
        p_amount: "50",
        p_type: "purchase",
        p_metadata: {},
        p_tier: "gifted",
        p_idempotency_key: "evt-1",
      });
      expect(result.tier).toBe("gifted");
    });

    it("sweepExpiredCredits threads p_dry_run and a non-null p_user_id into the expire_credits RPC body (lazy per-user sweep)", async () => {
      const captured = mockFetch({
        expired_count: 1,
        expired_amount: "25",
        expired_by_tier: { default: "25" },
        dry_run: false,
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.sweepExpiredCredits(false, "u1");

      expect(captured[0].url).toBe(`${URL_BASE}/rest/v1/rpc/expire_credits`);
      expect(captured[0].body).toEqual({
        p_dry_run: false,
        p_user_id: "u1",
      });
      expect(result.expiredAmount.toString()).toBe("25");
    });

    it("sweepExpiredCredits omits p_user_id (sends null) for a global sweep", async () => {
      const captured = mockFetch({
        expired_count: 0,
        expired_amount: "0",
        expired_by_tier: {},
        dry_run: true,
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await store.sweepExpiredCredits(true);

      expect(captured[0].body).toEqual({
        p_dry_run: true,
        p_user_id: null,
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
      mockFetch({
        user_id: "u1",
        plan_id: "p",
        plan_name: "P",
        free_allowance: "0",
        features: { quota: 0 },
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.checkFeature("u1", "quota");
      expect(result.value).toBe(0);
      expect(result.hasFeature).toBe(true);
    });

    it("treats explicit false as absent", async () => {
      mockFetch({
        user_id: "u1",
        plan_id: "p",
        plan_name: "P",
        free_allowance: "0",
        features: { flag: false },
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.checkFeature("u1", "flag");
      expect(result.hasFeature).toBe(false);
    });
  });

  // SB2 — HTTP 404 → StoreError
  describe("HTTP error codes → StoreError", () => {
    it("HTTP 404 response throws StoreError (SB2)", async () => {
      mockFetch("not found", 404);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.getBalance("u1")).rejects.toThrow(StoreError);
      await expect(store.getBalance("u1")).rejects.toThrow(/404/);
    });

    it("HTTP 503 response throws StoreError (SB3-extended)", async () => {
      mockFetch("service unavailable", 503);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.addCredits("u1", D(10))).rejects.toThrow(StoreError);
      await expect(store.addCredits("u1", D(10))).rejects.toThrow(/503/);
    });
  });

  // SB6 — metadata with undefined values: JSON.stringify drops them, so they
  // must not appear in the serialized body sent to fetch.
  describe("metadata serialization (SB6)", () => {
    it("deductWithAllowance omits undefined metadata keys from the request body", async () => {
      const captured = mockFetch({
        transaction_id: "tx-1",
        amount: "1",
        allowance_consumed: "0",
        balance_after: "99",
        idempotent: false,
        cap_warning: null,
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await store.deductWithAllowance("u1", D("1"), {
        metadata: { reason: undefined, model: "gpt-4" } as Record<string, unknown>,
      });
      const body = captured[0].body as Record<string, unknown>;
      const meta = body.p_metadata as Record<string, unknown>;
      // JSON.stringify drops `undefined` values — "reason" must not appear.
      expect(Object.prototype.hasOwnProperty.call(meta, "reason")).toBe(false);
      expect(meta.model).toBe("gpt-4");
    });
  });

  // H13a — HTTP 500 responses → StoreError with meaningful message
  describe("HTTP 500 responses → StoreError (H13a)", () => {
    it("getBalance throws StoreError with status in message on HTTP 500", async () => {
      mockFetch({ message: "Internal Server Error" }, 500);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.getBalance("u1")).rejects.toThrow(StoreError);
      await expect(store.getBalance("u1")).rejects.toThrow(/500/);
    });

    it("addCredits throws StoreError with status in message on HTTP 500", async () => {
      mockFetch({ message: "Internal Server Error" }, 500);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.addCredits("u1", D("10"))).rejects.toThrow(StoreError);
      await expect(store.addCredits("u1", D("10"))).rejects.toThrow(/500/);
    });

    it("deductWithAllowance throws StoreError with status in message on HTTP 500", async () => {
      mockFetch({ message: "Internal Server Error" }, 500);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await expect(store.deductWithAllowance("u1", D("5"))).rejects.toThrow(StoreError);
      await expect(store.deductWithAllowance("u1", D("5"))).rejects.toThrow(/500/);
    });

    it("thrown error is a StoreError instance (not a plain Error) on HTTP 500", async () => {
      mockFetch({ message: "Internal Server Error" }, 500);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      let thrown: unknown;
      try {
        await store.getBalance("u1");
      } catch (e) {
        thrown = e;
      }
      expect(thrown).toBeInstanceOf(StoreError);
    });
  });

  // H13b — network/timeout errors → StoreError (not untyped Error)
  describe("network error propagates as StoreError (H13b)", () => {
    it("getBalance wraps ECONNREFUSED as StoreError", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(() => Promise.reject(new Error("ECONNREFUSED"))),
      );
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      let thrown: unknown;
      try {
        await store.getBalance("u1");
      } catch (e) {
        thrown = e;
      }
      expect(thrown).toBeInstanceOf(StoreError);
      expect((thrown as StoreError).message).toMatch(/ECONNREFUSED/);
    });
  });

  // H13c — Decimal amounts serialized as strings, not JS numbers
  describe("Decimal JSON serialization (H13c)", () => {
    it("addCredits sends Decimal(0.0001) as the string '0.0001', not a number", async () => {
      const captured = mockFetch({
        id: "tx-1",
        user_id: "u1",
        amount: "0.0001",
        new_balance: "0.0001",
        lifetime_purchased: "0.0001",
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await store.addCredits("u1", D("0.0001"));

      const body = captured[0].body as Record<string, unknown>;
      // Must be string "0.0001", not the JS float 0.0001
      expect(body.p_amount).toBe("0.0001");
      expect(typeof body.p_amount).toBe("string");
    });

    it("deductWithAllowance sends tiny Decimal as a string in the request body", async () => {
      const captured = mockFetch({
        transaction_id: "tx-1",
        amount: "0.0001",
        allowance_consumed: "0",
        balance_after: "9.9999",
        idempotent: false,
        cap_warning: null,
      });
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await store.deductWithAllowance("u1", D("0.0001"));

      const body = captured[0].body as Record<string, unknown>;
      expect(body.p_amount).toBe("0.0001");
      expect(typeof body.p_amount).toBe("string");
    });
  });

  // M18 — URL malformation handling
  describe("URL malformation (M18)", () => {
    it("double trailing slash is normalized — no double slash in request URL", async () => {
      const captured = mockFetch({ user_id: "u1", balance: "0", lifetime_purchased: "0" });
      const store = new HttpxSupabaseStore("https://example.supabase.co//", KEY);
      await store.getBalance("u1");
      expect(captured[0].url).toBe("https://example.supabase.co/rest/v1/rpc/get_credits_balance");
      // No double-slash in the path part (exclude the https:// protocol)
      expect(captured[0].url.replace(/^https:\/\//, "")).not.toMatch(/\/\//);
    });

    it("URL with path appends RPC path without introducing double-slash", async () => {
      const captured = mockFetch({ user_id: "u1", balance: "0", lifetime_purchased: "0" });
      // The constructor strips trailing slashes; a path like /some/path stays as-is.
      // The RPC endpoint is appended after the stored URL — verify no double-slash is introduced.
      const store = new HttpxSupabaseStore("https://example.supabase.co/some/path", KEY);
      await store.getBalance("u1");
      // The resulting URL should be the stored base + /rest/v1/rpc/...
      expect(captured[0].url).toBe(
        "https://example.supabase.co/some/path/rest/v1/rpc/get_credits_balance",
      );
      // No double-slash anywhere after the protocol
      expect(captured[0].url.replace(/^https:\/\//, "")).not.toMatch(/\/\//);
    });
  });

  // Large response / pagination — listUserTransactions with 100 rows
  describe("large response / pagination", () => {
    it("listUserTransactions returns all 100 rows with correct types", async () => {
      const rows = Array.from({ length: 100 }, (_, i) => ({
        id: `tx-${i}`,
        user_id: "u1",
        amount: (-(i + 1) * 0.1).toFixed(4),
        type: "deduction",
        reference_type: "usage",
        reference_id: `ref-${i}`,
        metadata: { model: "gpt-4" },
        created_at: new Date(2024, 0, 1, 0, i).toISOString(),
        total_count: 100,
      }));

      mockFetch(rows);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      const result = await store.listUserTransactions("u1", { limit: 100 });

      expect(result.items).toHaveLength(100);
      expect(result.total).toBe(100);

      // Spot-check first and last items for correct typing — no truncation, no undefined fields
      const first = result.items[0];
      expect(first.id).toBe("tx-0");
      expect(first.userId).toBe("u1");
      expect(first.amount).toBeInstanceOf(Decimal);
      expect(first.type).toBe("deduction");
      expect(first.referenceType).toBe("usage");
      expect(first.referenceId).toBe("ref-0");
      expect(first.metadata).toEqual({ model: "gpt-4" });
      expect(typeof first.createdAt).toBe("string");

      const last = result.items[99];
      expect(last.id).toBe("tx-99");
      expect(last.amount).toBeInstanceOf(Decimal);
      expect(last.referenceId).toBe("ref-99");

      // Verify none of the items have undefined fields
      for (const item of result.items) {
        expect(item.id).toBeDefined();
        expect(item.userId).toBeDefined();
        expect(item.amount).toBeInstanceOf(Decimal);
        expect(item.type).toBeDefined();
        expect(item.createdAt).toBeDefined();
      }
    });

    it("listUserTransactions sends correct params when limit is specified", async () => {
      const captured = mockFetch([]);
      const store = new HttpxSupabaseStore(URL_BASE, KEY);
      await store.listUserTransactions("u1", { limit: 100, offset: 0 });

      expect(captured[0].url).toBe(`${URL_BASE}/rest/v1/rpc/list_user_transactions`);
      const body = captured[0].body as Record<string, unknown>;
      expect(body.p_user_id).toBe("u1");
      expect(body.p_limit).toBe(100);
      expect(body.p_offset).toBe(0);
    });
  });
});
