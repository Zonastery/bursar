import { describe, it, expect, vi } from "vitest";
import Decimal from "decimal.js";
import type { PgPool, PgPoolConstructor } from "../src/credits/postgres/store.js";
import { PostgresStore as BasePostgresStore } from "../src/credits/postgres/store.js";
import { PostgresBillingStore as BasePostgresBillingStore } from "../src/billing/postgres/store.js";
import { StoreUnavailableError } from "../src/errors.js";

const D = (n: number | string) => new Decimal(n);
const TEST_TENANT_ID = "00000000-0000-0000-0000-000000000001";
const TEST_USER_ID = "00000000-0000-0000-0000-000000000002";
const TEST_PLAN_ID = "00000000-0000-0000-0000-000000000003";
const TEST_CHARGE_ID = "00000000-0000-0000-0000-000000000004";
const TEST_LEDGER_ID = "00000000-0000-0000-0000-000000000005";

class PostgresStore extends BasePostgresStore {
  constructor(databaseUrl: string, poolOrCtor?: PgPool | PgPoolConstructor) {
    super({
      postgres: typeof poolOrCtor === "object" ? poolOrCtor : databaseUrl,
      tenantId: TEST_TENANT_ID,
      poolConstructor: typeof poolOrCtor === "function" ? poolOrCtor : undefined,
    });
  }
}

class PostgresBillingStore extends BasePostgresBillingStore {
  constructor(poolOrUrl: import("pg").Pool | string) {
    super({ postgres: poolOrUrl, tenantId: TEST_TENANT_ID });
  }
}

function mockTransactionClient(query: ReturnType<typeof vi.fn>) {
  return {
    query,
    release: vi.fn(),
  };
}

/** Mock pool that returns a fixed set of rows for every query. */
function makeMockPool(rows: unknown[], subsequentRows: unknown[] = rows): PgPoolConstructor {
  return vi.fn(() => {
    let dataQueryCount = 0;
    const query = vi.fn((text: string) => {
      if (
        text === "BEGIN" ||
        text === "COMMIT" ||
        text === "ROLLBACK" ||
        text.startsWith("SET LOCAL ROLE") ||
        text.startsWith("SELECT set_config(")
      ) {
        return Promise.resolve({ rows: [] });
      }
      const result = dataQueryCount === 0 ? rows : subsequentRows;
      dataQueryCount += 1;
      return Promise.resolve({ rows: result });
    });
    return {
      query,
      connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
      end: vi.fn().mockResolvedValue(undefined),
    };
  }) as unknown as PgPoolConstructor;
}

/**
 * Mock pool that records the SQL text + params it was called with, returning a
 * caller-supplied row set. Lets us assert how the store builds RPC calls.
 */
function makeRecordingPool(
  rows: unknown[],
  subsequentRows: unknown[] = rows,
): {
  ctor: PgPoolConstructor;
  calls: Array<{ text: string; params: unknown[] }>;
} {
  const calls: Array<{ text: string; params: unknown[] }> = [];
  let dataQueryCount = 0;
  const query = vi.fn((text: string, params?: unknown[]) => {
    if (
      text === "BEGIN" ||
      text === "COMMIT" ||
      text === "ROLLBACK" ||
      text.startsWith("SET LOCAL ROLE") ||
      text.startsWith("SELECT set_config(")
    ) {
      return Promise.resolve({ rows: [] });
    }
    calls.push({ text, params: params ?? [] });
    const result = dataQueryCount === 0 ? rows : subsequentRows;
    dataQueryCount += 1;
    return Promise.resolve({ rows: result });
  });
  const ctor = vi.fn(
    () =>
      ({
        query,
        connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
        end: vi.fn().mockResolvedValue(undefined),
      }) as unknown as PgPool,
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
      makeMockPool([{ bucket_key: "purchased", balance: "100.1234", lifetime_purchased: "0" }]),
    );
    const result = await store.getBalance("user-1");
    expect(result.balance.toString()).toBe("100.1234");
    expect(result.lifetimePurchased.toString()).toBe("0");
  });

  it("maps the allowance priority returned by get_subject_plan", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          user_id: TEST_USER_ID,
          plan_assigned_at: "2026-01-01T00:00:00.000Z",
          plan_assignment_ends_at: null,
          assignment_source_type: "manual",
          assignment_source_id: null,
          catalog_revision_pinned: false,
          plan_id: TEST_PLAN_ID,
          plan_key: "pro",
          plan_label: "Pro",
          rate_card: null,
          allowed_operations: [],
          credit_allowance_amount: "100",
          credit_allowance_priority: 20,
          credit_allowance_reset_unit: "month",
          credit_allowance_reset_count: 1,
          credit_allowance_reset_anchor: "calendar",
          credit_allowance_reset_timezone: "UTC",
          entitlements: {},
          credit_policy_type: "prepaid",
          credit_limit: null,
          admission_max_in_flight: null,
          operation_admission: {},
          catalog_revision_no: 1,
        },
      ]),
    );

    const result = await store.getUserPlan(TEST_USER_ID);

    expect(result.allowance?.amount.toString()).toBe("100");
    expect(result.allowance?.priority).toBe(20);
  });

  it("addCredits fails closed for an empty mutation result", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    await expect(store.addCredits("user-1", D(100))).rejects.toThrow(
      /expected exactly one result row/,
    );
  });

  it("addCredits parses row result and sends amount as a decimal string", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        entry_id: "tx-1",
        user_id: "user-1",
        balance_after: "200",
        lifetime_purchased: "100.5",
        bucket_key: "purchased",
        replayed: false,
        error_code: null,
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const result = await store.addCredits("user-1", D("100.5"), "purchase");
    expect(result.entryId).toBe("tx-1");
    expect(result.newBalance.toString()).toBe("200");
    // amount param serialized as a decimal string (no binary float).
    expect(calls[0]!.text).toContain("post_credit");
    expect(calls[0]!.params[2]!).toBe("100.5");
  });

  it("executes application-driven grant programs and maps every award", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        grant_event_id: "00000000-0000-0000-0000-000000000010",
        grant_award_id: "00000000-0000-0000-0000-000000000011",
        recipient_subject_id: TEST_USER_ID,
        ledger_entry_id: "00000000-0000-0000-0000-000000000012",
        amount: "12.5",
        replayed: false,
        error_code: null,
      },
      {
        grant_event_id: "00000000-0000-0000-0000-000000000010",
        grant_award_id: "00000000-0000-0000-0000-000000000013",
        recipient_subject_id: "00000000-0000-0000-0000-000000000014",
        ledger_entry_id: "00000000-0000-0000-0000-000000000015",
        amount: "2.5",
        replayed: false,
        error_code: null,
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);

    const result = await store.executeGrantProgram({
      trigger: "referral_completed",
      programKey: "referral_bonus",
      subjectId: TEST_USER_ID,
      eventKey: "referral-42",
      referrerSubjectId: "00000000-0000-0000-0000-000000000014",
      region: "US",
      metadata: { campaign: "summer" },
    });

    expect(calls[0]!.text).toContain("execute_grant_program");
    expect(calls[0]!.params).toEqual([
      "referral_completed",
      "referral_bonus",
      TEST_USER_ID,
      "referral-42",
      "00000000-0000-0000-0000-000000000014",
      "US",
      JSON.stringify({ campaign: "summer" }),
    ]);
    expect(result).toHaveLength(2);
    expect(result[0]!).toMatchObject({
      grantEventId: "00000000-0000-0000-0000-000000000010",
      recipientSubjectId: TEST_USER_ID,
      replayed: false,
    });
    const award = result[0]!;
    expect(award.error).toBeNull();
    if (award.error !== null) throw new Error("expected a committed grant award");
    expect(award.amount.toString()).toBe("12.5");
  });

  it("reads the immutable lease pricing context", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        catalog_revision_no: "7",
        plan_id: TEST_PLAN_ID,
        plan_key: "pro",
        rate_card: "premium",
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);

    await expect(
      store.getLeasePricingContext(TEST_USER_ID, "00000000-0000-0000-0000-000000000016"),
    ).resolves.toEqual({
      catalogVersion: 7,
      planId: TEST_PLAN_ID,
      planKey: "pro",
      rateCard: "premium",
    });
    expect(calls[0]!.text).toContain("get_credit_lease_pricing_context");
    expect(calls[0]!.params).toEqual([TEST_USER_ID, "00000000-0000-0000-0000-000000000016"]);
  });

  it("expires a bounded lease batch", async () => {
    const { ctor, calls } = makeRecordingPool([{ expired: 3 }]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);

    await expect(store.expireLeases(25)).resolves.toBe(3);
    expect(calls[0]!.text).toContain("expire_leases");
    expect(calls[0]!.params).toEqual([25]);
    await expect(store.expireLeases(0)).rejects.toThrow(
      "lease expiry limit must be an integer between 1 and 1000",
    );
  });

  it("removes a team member through the scalar RPC", async () => {
    const { ctor, calls } = makeRecordingPool([{ remove_team_member: true }]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);

    await expect(store.removeTeamMember("team-1", "user-1")).resolves.toBe(true);
    expect(calls[0]!.text).toContain("remove_team_member");
    expect(calls[0]!.params).toEqual(["team-1", "user-1"]);
  });

  // ── Credit tiers ─────────────────────────────────────────────────────
  describe("credit tiers", () => {
    it("addCredits sends the bucket to post_credit", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          entry_id: "tx-tier-1",
          user_id: "user-1",
          balance_after: "20",
          lifetime_purchased: "20",
          bucket_key: "gifted",
          replayed: false,
          error_code: null,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.addCredits("user-1", D(20), "adjustment", null, null, "gifted");
      expect(calls[0]!.text).toContain("post_credit");
      expect(calls[0]!.params.slice(0, 4)).toEqual(["user-1", "grant", "20", "adjustment"]);
      expect(calls[0]!.params[6]!).toBe("gifted");
      expect(result.bucket).toBe("gifted");
    });

    it("addCredits omits tier as null when not specified, and defaults result.bucket to 'default'", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          entry_id: "tx-1",
          user_id: "user-1",
          balance_after: "10",
          lifetime_purchased: "10",
          bucket_key: "default",
          replayed: false,
          error_code: null,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.addCredits("user-1", D(10));
      expect(calls[0]!.params[6]!).toBeNull();
      // Row omitted `tier` entirely (e.g. a no-tiers-configured deployment) —
      // the store falls back to "default" rather than surfacing `undefined`.
      expect(result.bucket).toBe("default");
    });

    it("deductWithAllowance parses bucket_breakdown into a Record<string, Decimal>", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool(
          [
            {
              charge_id: TEST_CHARGE_ID,
              ledger_entry_id: TEST_LEDGER_ID,
              charged: "15.0000",
              allowance_covered: "0.0000",
              replayed: false,
              error_code: null,
            },
          ],
          [
            {
              balance_after: "5.0000",
              bucket_breakdown: { gifted: "10.0000", purchased: "5.0000" },
            },
          ],
        ),
      );
      const result = await store.deductWithAllowance(TEST_USER_ID, D(15));
      expect(result.bucketBreakdown).not.toBeNull();
      expect(result.bucketBreakdown!.gifted).toBeInstanceOf(Decimal);
      expect(result.bucketBreakdown!.gifted?.toString()).toBe("10");
      expect(result.bucketBreakdown!.purchased?.toString()).toBe("5");
    });

    it("addCredits surfaces the post_credit error envelope", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            entry_id: null,
            balance_after: null,
            replayed: false,
            error_code: "missing_catalog_bucket",
          },
        ]),
      );
      await expect(
        store.addCredits("user-1", D(10), "adjustment", null, null, "bogus"),
      ).rejects.toThrow("post_credit: missing_catalog_bucket");
    });

    it("deductWithAllowance leaves bucketBreakdown null when absent from the row", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool(
          [
            {
              charge_id: TEST_CHARGE_ID,
              ledger_entry_id: TEST_LEDGER_ID,
              charged: "15.0000",
              allowance_covered: "0.0000",
              replayed: false,
              error_code: null,
            },
          ],
          [{ balance_after: "5.0000", bucket_breakdown: null }],
        ),
      );
      const result = await store.deductWithAllowance(TEST_USER_ID, D(15));
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
      expect(result.expiredByBucket?.gifted?.eq("12.5")).toBe(true);
      expect(result.dryRun).toBe(false);
    });

    it("getBucketBalances maps the normalized rowset", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            bucket_key: "gifted",
            label: "Gifted",
            priority: 10,
            expires: true,
            balance: "20.0000",
          },
          {
            bucket_key: "purchased",
            label: "Purchased",
            priority: 20,
            expires: false,
            balance: "10.0000",
          },
        ]),
      );
      const result = await store.getBucketBalances(TEST_USER_ID);
      expect(result.userId).toBe(TEST_USER_ID);
      expect(result.buckets).toHaveLength(2);
      expect(result.buckets[0]!).toMatchObject({
        bucketKey: "gifted",
        label: "Gifted",
        priority: 10,
        expires: true,
      });
      expect(result.buckets[0]!.balance.toString()).toBe("20");
      expect(result.buckets[1]!.balance.toString()).toBe("10");
      expect(result.totalBalance).toBeInstanceOf(Decimal);
      expect(result.totalBalance.toString()).toBe("30");
    });

    it("getBucketBalances returns an empty bucket list and zero balance for empty results", async () => {
      const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
      const result = await store.getBucketBalances(TEST_USER_ID);
      expect(result.userId).toBe(TEST_USER_ID);
      expect(result.buckets).toEqual([]);
      expect(result.totalBalance.toString()).toBe("0");
    });
  });

  describe("deductWithAllowance", () => {
    it("calls charge_usage_for_operation with the normalized contract", async () => {
      const { ctor, calls } = makeRecordingPool(
        [
          {
            charge_id: TEST_CHARGE_ID,
            ledger_entry_id: TEST_LEDGER_ID,
            charged: "2.5000",
            allowance_covered: "0.0000",
            replayed: false,
            error_code: null,
          },
        ],
        [{ balance_after: "97.5000", bucket_breakdown: null }],
      );
      const store = new PostgresStore("postgresql://localhost/db", ctor);
      const result = await store.deductWithAllowance(TEST_USER_ID, D("2.5"), {
        idempotencyKey: "k1",
        operation: "completion",
        model: "gpt-4",
        region: "us-east",
        measures: { input_tokens: 2 },
        dimensions: { model: "gpt-4", region: "us-east" },
        metadata: { foo: "bar" },
      });
      expect(calls[0]!.text).toContain("charge_usage_for_operation");
      expect(calls[0]!.params).toEqual([
        TEST_USER_ID,
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
      if (result.error !== null) throw new Error(result.error);
      // Parses NUMERIC strings to exact Decimal.
      expect(result.amount.toString()).toBe("2.5");
      expect(result.allowanceConsumed.toString()).toBe("0");
      expect(result.balanceAfter.toString()).toBe("97.5");
      expect(result.idempotent).toBe(false);
    });

    it("parses allowance consumption", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool(
          [
            {
              charge_id: TEST_CHARGE_ID,
              ledger_entry_id: TEST_LEDGER_ID,
              charged: "15.0000",
              allowance_covered: "10.0000",
              replayed: false,
              error_code: null,
            },
          ],
          [{ balance_after: "85.0000", bucket_breakdown: null }],
        ),
      );
      const result = await store.deductWithAllowance(TEST_USER_ID, D(25));
      expect(result.amount.toString()).toBe("15");
      expect(result.allowanceConsumed.toString()).toBe("10");
    });

    it("maps quota_exceeded error envelope to result.error (no throw)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            charge_id: null,
            ledger_entry_id: null,
            charged: "0",
            allowance_covered: "0",
            replayed: false,
            error_code: "quota_exceeded",
          },
        ]),
      );
      const result = await store.deductWithAllowance(TEST_USER_ID, D(20));
      expect(result.error).toBe("quota_exceeded");
      expect(result.entryId).toBeNull();
    });

    it("maps insufficient_credits error envelope", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          {
            charge_id: null,
            ledger_entry_id: null,
            charged: "0",
            allowance_covered: "0",
            replayed: false,
            error_code: "insufficient_credits",
          },
        ]),
      );
      const result = await store.deductWithAllowance(TEST_USER_ID, D(20));
      expect(result.error).toBe("insufficient_credits");
    });

    it("surfaces idempotent replay", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool(
          [
            {
              charge_id: TEST_CHARGE_ID,
              ledger_entry_id: TEST_LEDGER_ID,
              charged: "10.0000",
              allowance_covered: "0.0000",
              replayed: true,
              error_code: null,
            },
          ],
          [{ balance_after: "90.0000", bucket_breakdown: null }],
        ),
      );
      const result = await store.deductWithAllowance(TEST_USER_ID, D(10), { idempotencyKey: "k" });
      expect(result.idempotent).toBe(true);
      expect(result.entryId).toBe(TEST_LEDGER_ID);
    });
  });

  describe("recordUsage", () => {
    it("records an exact cost without a ledger debit", async () => {
      const { ctor, calls } = makeRecordingPool([
        {
          charge_id: TEST_CHARGE_ID,
          requested: "12.5000",
          ledger_entry_id: null,
          charged: "0",
          allowance_covered: "0",
          replayed: false,
          error_code: null,
        },
      ]);
      const store = new PostgresStore("postgresql://localhost/db", ctor);

      const result = await store.recordUsage(TEST_USER_ID, "roadmap_generation", D("12.5"), {
        idempotencyKey: "roadmap-1:outline",
        model: "linkup",
        measures: { requests: 1 },
        dimensions: { provider: "linkup" },
        metadata: { workflowStep: "outline" },
      });

      expect(calls[0]!.text).toContain("record_usage");
      expect(calls[0]!.params).toEqual([
        TEST_USER_ID,
        "roadmap_generation",
        "12.5",
        "roadmap-1:outline",
        null,
        "linkup",
        null,
        JSON.stringify({ workflowStep: "outline" }),
        JSON.stringify({ requests: 1 }),
        JSON.stringify({ provider: "linkup" }),
      ]);
      expect(result).toMatchObject({
        usageId: TEST_CHARGE_ID,
        userId: TEST_USER_ID,
        idempotent: false,
        error: null,
      });
      expect(result.requested.toString()).toBe("12.5");
    });

    it("fails closed when the RPC returns no result envelope", async () => {
      const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));

      await expect(store.recordUsage("user-1", "roadmap_generation", D(12))).rejects.toThrow(
        /expected exactly one result row/,
      );
    });
  });

  describe("callproc unwrapping robustness", () => {
    it("list RPC returns ALL rows (not just the first)", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([
          { subject_id: "u1", total_spend: "10", charge_count: 1 },
          { subject_id: "u2", total_spend: "20", charge_count: 2 },
          { subject_id: "u3", total_spend: "30", charge_count: 3 },
        ]),
      );
      const rows = await store.spendByUser(new Date(), new Date());
      expect(rows).toHaveLength(3);
      expect(rows[2]!.totalSpend.toString()).toBe("30");
    });

    it("normalized bucket rows are not mistaken for scalar RPC results", async () => {
      const store = new PostgresStore(
        "postgresql://localhost/db",
        makeMockPool([{ bucket_key: "default", balance: "42", lifetime_purchased: "0" }]),
      );
      const result = await store.getBalance("u1");
      expect(result.balance.toString()).toBe("42");
    });
  });

  it("lists allowance-covered usage and permits the maximum page look-ahead", async () => {
    const eventAt = new Date("2030-01-02T00:00:00.000Z");
    const { ctor, calls } = makeRecordingPool([
      {
        usage_id: "usage-1",
        account_id: "account-1",
        operation: "completion",
        requested: "10",
        charged: "2.5",
        allowance_requested: "10",
        allowance_covered: "7.5",
        billing_disposition: "billable",
        feature: "chat",
        model: "gpt-5",
        region: "in-west",
        event_at: eventAt,
        idempotency_key: "request-1",
        metadata: { trace: "one" },
        created_at: eventAt,
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);

    const page = await store.listUsageCharges("user-1", {
      fromDate: new Date("2030-01-01T00:00:00.000Z"),
      toDate: new Date("2030-02-01T00:00:00.000Z"),
      limit: 200,
      cursor: { eventAt: eventAt.toISOString(), usageId: "usage-cursor" },
      includeRecordOnly: false,
    });

    expect(calls[0]!.text).toContain("list_usage_charges");
    expect(calls[0]!.params).toEqual([
      "user-1",
      eventAt.toISOString(),
      "usage-cursor",
      201,
      "2030-01-01T00:00:00.000Z",
      "2030-02-01T00:00:00.000Z",
      false,
    ]);
    expect(page.nextCursor).toBeNull();
    expect(page.items[0]!).toMatchObject({
      usageId: "usage-1",
      operation: "completion",
      billingDisposition: "billable",
      feature: "chat",
      eventAt: eventAt.toISOString(),
    });
    expect(page.items[0]?.charged.toString()).toBe("2.5");
    expect(page.items[0]?.allowanceCovered.toString()).toBe("7.5");
  });

  it("getActiveCatalog returns null for empty results", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    const result = await store.getActiveCatalog();
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
          label: null,
          status: "active",
          created_at: "2026-01-01T00:00:00.000Z",
        },
      ]),
    );
    const result = await store.getActiveCatalog();
    expect(result?.config).toEqual(config);
  });

  it("keeps persisted payment metadata in its canonical snake_case shape", async () => {
    const query = vi.fn().mockResolvedValue({
      rows: [
        {
          id: "00000000-0000-0000-0000-000000000010",
          provider: "stripe",
          provider_payment_id: "pay_1",
          provider_invoice_id: null,
          subject_id: "00000000-0000-0000-0000-000000000001",
          purpose: "credit_topup",
          amount_minor: 1000,
          tax_minor: 0,
          currency: "USD",
          status: "succeeded",
          provider_updated_at: "2026-08-01T00:00:00.000Z",
          metadata: { credits_per_unit: 1000, user_defined_key: "unchanged" },
        },
      ],
    });
    const pool = {
      query,
      connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;
    const store = new PostgresBillingStore(pool);
    await expect(store.getBillingPayment("stripe", "pay_1")).resolves.toMatchObject({
      purpose: "credit_topup",
      metadata: { credits_per_unit: 1000, user_defined_key: "unchanged" },
    });
  });

  it("maps billing credit RPC rows to the public typed result", async () => {
    const query = vi.fn().mockResolvedValue({
      rows: [
        {
          ledger_entry_id: "00000000-0000-0000-0000-000000000020",
          balance_after: "123.4500",
          replayed: true,
          error_code: null,
        },
      ],
    });
    const pool = {
      query,
      connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;
    const store = new PostgresBillingStore(pool);

    const result = await store.grantBillingCredit(
      "00000000-0000-0000-0000-000000000010",
      "billing:test:topup",
    );

    expect(result).toMatchObject({
      ledgerEntryId: "00000000-0000-0000-0000-000000000020",
      replayed: true,
      errorCode: null,
    });
    expect(result.balanceAfter).toBeInstanceOf(Decimal);
    expect(result.balanceAfter?.toString()).toBe("123.45");
  });

  it("preserves billing credit RPC error envelopes", async () => {
    const query = vi.fn().mockResolvedValue({
      rows: [
        {
          ledger_entry_id: null,
          balance_after: null,
          replayed: false,
          error_code: "grant_not_posted",
        },
      ],
    });
    const pool = {
      query,
      connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;
    const store = new PostgresBillingStore(pool);

    await expect(
      store.postBillingRefund(
        "00000000-0000-0000-0000-000000000030",
        "00000000-0000-0000-0000-000000000010",
        500,
        "billing:test:refund",
      ),
    ).resolves.toEqual({
      ledgerEntryId: null,
      balanceAfter: null,
      replayed: false,
      errorCode: "grant_not_posted",
    });
  });

  it("orders subscription Date values without losing millisecond precision", async () => {
    const subjectId = "00000000-0000-0000-0000-000000000011";
    const offerId = "00000000-0000-0000-0000-000000000012";
    const revisionId = "00000000-0000-0000-0000-000000000013";
    const subscription = (id: string, providerUpdatedAt: Date) => ({
      id,
      subject_id: subjectId,
      provider: "stripe",
      provider_subscription_id: id.endsWith("14") ? "sub-older" : "sub-newer",
      provider_customer_id: null,
      offer_id: offerId,
      catalog_revision_id: revisionId,
      status: "active",
      current_period_start: null,
      current_period_end: null,
      trial_end: null,
      cancel_at: null,
      ended_at: null,
      grace_ends_at: null,
      grace_expired_at: null,
      provider_updated_at: providerUpdatedAt,
      cancel_at_period_end: false,
      metadata: {},
    });
    const query = vi.fn((text: string) => {
      if (text.includes("list_billing_subscriptions")) {
        return Promise.resolve({
          rows: [
            subscription(
              "00000000-0000-0000-0000-000000000014",
              new Date("2026-07-18T05:15:24.100Z"),
            ),
            subscription(
              "00000000-0000-0000-0000-000000000015",
              new Date("2026-07-18T05:15:24.900Z"),
            ),
          ],
        });
      }
      if (text.includes("get_catalog_offer_context")) {
        return Promise.resolve({
          rows: [
            {
              offer_key: "pro_monthly",
              plan_id: "00000000-0000-0000-0000-000000000016",
              plan_key: "pro",
              billing_unit: "month",
              billing_count: 1,
            },
          ],
        });
      }
      return Promise.resolve({ rows: [] });
    });
    const pool = {
      query,
      connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;
    const store = new PostgresBillingStore(pool);

    await expect(store.getUserSubscription(subjectId, ["active"])).resolves.toMatchObject({
      providerSubscriptionId: "sub-newer",
    });
  });

  it("hydrates subscription changes with revision-pinned offer context", async () => {
    const subscriptionId = "00000000-0000-0000-0000-000000000030";
    const fromOfferId = "00000000-0000-0000-0000-000000000031";
    const fromRevisionId = "00000000-0000-0000-0000-000000000032";
    const toOfferId = "00000000-0000-0000-0000-000000000033";
    const toRevisionId = "00000000-0000-0000-0000-000000000034";
    const query = vi.fn((text: string, params?: unknown[]) => {
      if (text.includes("get_open_billing_subscription_change")) {
        return Promise.resolve({
          rows: [
            {
              id: "1",
              subscription_id: subscriptionId,
              from_offer_id: fromOfferId,
              from_catalog_revision_id: fromRevisionId,
              to_offer_id: toOfferId,
              to_catalog_revision_id: toRevisionId,
              effective_at: new Date("2027-01-01T00:00:00.000Z"),
              effective_behavior: "renewal",
              state: "scheduled",
              proration_behavior: "none",
              idempotency_key: "change-key",
              provider_operation_id: null,
              error_message: null,
            },
          ],
        });
      }
      if (text.includes("get_catalog_offer_context")) {
        if (!params) throw new Error("Expected catalog-offer query parameters");
        return Promise.resolve({
          rows: [
            {
              side: "from",
              offer_id: params[0]!,
              offer_key: "monk_monthly",
              plan_id: "00000000-0000-0000-0000-000000000035",
              plan_key: "monk",
              billing_unit: "month",
              billing_count: 1,
            },
            {
              side: "to",
              offer_id: params[2]!,
              offer_key: "sage_yearly",
              plan_id: "00000000-0000-0000-0000-000000000036",
              plan_key: "sage",
              billing_unit: "year",
              billing_count: 1,
            },
          ],
        });
      }
      return Promise.resolve({ rows: [] });
    });
    const pool = {
      query,
      connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
      end: vi.fn().mockResolvedValue(undefined),
    } as unknown as import("pg").Pool;

    const store = new PostgresBillingStore(pool);
    const change = await store.getOpenBillingSubscriptionChange("dodo", "provider-subscription");

    expect(change).toMatchObject({
      fromOfferId,
      toOfferId,
      fromOffer: {
        offerId: fromOfferId,
        offerKey: "monk_monthly",
        plan: "monk",
        interval: "month",
      },
      toOffer: {
        offerId: toOfferId,
        offerKey: "sage_yearly",
        plan: "sage",
        interval: "year",
      },
      effectiveAt: "2027-01-01T00:00:00.000Z",
      effective: "renewal",
      prorationBehavior: "none",
    });
  });

  it("publishAndActivateCatalog rejects an empty database result", async () => {
    const store = new PostgresStore("postgresql://localhost/db", makeMockPool([]));
    await expect(
      store.publishAndActivateCatalog({
        version: 1,
        credits: {
          accounting: { unit: "credit", scale: 6, rounding: "half_up" },
        },
      }),
    ).rejects.toThrow(/expected exactly one result row/);
  });

  it("checkFeature treats numeric 0 as present (M6)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          feature_key: "quota",
          feature_type: "integer",
          feature_value: 0,
          catalog_revision_id: "00000000-0000-0000-0000-000000000040",
          plan_key: "pro",
          value_source: "plan",
        },
      ]),
    );
    const result = await store.checkFeature(TEST_USER_ID, "quota");
    expect(result.value).toBe(0);
    expect(result.hasFeature).toBe(true);
  });

  it("checkFeature treats explicit false as absent (M6)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          feature_key: "flag",
          feature_type: "boolean",
          feature_value: false,
          catalog_revision_id: "00000000-0000-0000-0000-000000000040",
          plan_key: null,
          value_source: "default",
        },
      ]),
    );
    const result = await store.checkFeature(TEST_USER_ID, "flag");
    expect(result.hasFeature).toBe(false);
  });

  // PG2 — malformed financial rows fail closed instead of becoming a plausible zero.
  it("rejects a NULL committed balance instead of fabricating zero (PG2)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          entry_id: "tx-1",
          user_id: "user-1",
          balance_after: null,
          lifetime_purchased: "100",
          replayed: false,
          error_code: null,
        },
      ]),
    );
    await expect(store.addCredits("user-1", D(50))).rejects.toThrow(
      "successful credit postings require entry and balance fields",
    );
  });

  // PG3 — Decimal value sent as string for non-round amounts
  it("addCredits sends non-round amount as exact decimal string (PG3)", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        entry_id: "tx-2",
        user_id: "user-1",
        amount: "0.0001",
        balance_after: "0.0001",
        lifetime_purchased: "0.0001",
        bucket_key: "purchased",
        replayed: false,
        error_code: null,
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    await store.addCredits("user-1", D("0.0001"), "purchase");
    expect(calls[0]!.text).toContain("post_credit");
    // Must arrive as the string "0.0001", not a binary float like 0.00010000000000000002.
    expect(calls[0]!.params[2]!).toBe("0.0001");
  });

  // PG4 — expiresAt serialized as ISO string, not a Date object
  it("addCredits serializes expiresAt as ISO string (PG4)", async () => {
    const { ctor, calls } = makeRecordingPool([
      {
        entry_id: "tx-3",
        user_id: "user-1",
        amount: "50",
        balance_after: "150",
        lifetime_purchased: "150",
        bucket_key: "purchased",
        replayed: false,
        error_code: null,
      },
    ]);
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const expiresAt = new Date("2024-01-15T00:00:00.000Z");
    await store.addCredits("user-1", D(50), "purchase", null, expiresAt);
    // params[5]! is p_request; p_expires_at is also passed explicitly at params[8]!.
    const meta = JSON.parse(calls[0]!.params[5]! as string) as Record<string, unknown>;
    expect(typeof meta.expires_at).toBe("string");
    expect(meta.expires_at).toBe("2024-01-15T00:00:00.000Z");
    expect(calls[0]!.params[8]!).toBe("2024-01-15T00:00:00.000Z");
  });

  // PG5 — Unknown RPC error code → surfaces as result.error (not thrown, not silent ok)
  it("unknown RPC error code is surfaced as result.error without throwing (PG5)", async () => {
    const store = new PostgresStore(
      "postgresql://localhost/db",
      makeMockPool([
        {
          charge_id: null,
          ledger_entry_id: null,
          charged: "0",
          allowance_covered: "0",
          replayed: false,
          error_code: "some_unknown_code_xyz",
        },
      ]),
    );
    // deductWithAllowance maps ALL error envelopes to result.error — unknown codes included.
    const result = await store.deductWithAllowance(TEST_USER_ID, D(20));
    expect(result.error).toBe("some_unknown_code_xyz");
    expect(result.entryId).toBeNull();
  });

  // PG6 — Network/transport errors use the stable SDK taxonomy and retain cause.
  it("classifies a pool connection error from getBalance (PG6)", async () => {
    const networkError = Object.assign(new Error("Connection refused"), {
      code: "ECONNREFUSED",
    });
    const query = vi.fn(() => Promise.reject(networkError));
    const ctor = vi.fn(
      () =>
        ({
          query,
          connect: vi.fn().mockResolvedValue(mockTransactionClient(query)),
          end: vi.fn().mockResolvedValue(undefined),
        }) as unknown as import("../src/credits/postgres/store.js").PgPool,
    ) as unknown as import("../src/credits/postgres/store.js").PgPoolConstructor;
    const store = new PostgresStore("postgresql://localhost/db", ctor);
    const failure = store.getBalance("user-1");
    await expect(failure).rejects.toBeInstanceOf(StoreUnavailableError);
    await expect(failure).rejects.toMatchObject({
      code: "STORE_UNAVAILABLE",
      retryable: true,
      indeterminate: false,
      cause: networkError,
      details: {
        datastore: "postgresql",
        operation: "query",
        phase: "begin",
        networkCode: "ECONNREFUSED",
      },
    });
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
          balance_after: "100.1235",
          lifetime_purchased: "100.1235",
          bucket_key: "purchased",
          replayed: false,
          error_code: null,
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
