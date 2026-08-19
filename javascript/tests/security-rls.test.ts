import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { BillingService, PostgresBillingStore } from "../src/billing/index.js";
import type { BursarConfigData } from "../src/config.js";
import { CreditsService } from "../src/credits/service.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const SECOND_TENANT_ID = "00000000-0000-0000-0000-000000000002";
const TENANT_ISOLATION_SUBJECT_ID = "00000000-0000-0000-0000-000000000201";
const SUSPENDED_TENANT_SUBJECT_ID = "00000000-0000-0000-0000-000000000202";
const REFUND_ISOLATION_SUBJECT_ID = "00000000-0000-0000-0000-000000000203";
const REFUND_PROVIDER = "stripe";
const REFUND_TOPUP_PRICE_ID = "price_tenant_isolation_topup";

function catalogConfig(displayName: string): BursarConfigData {
  return {
    version: 1,
    catalog: { default_plan: "pro" },
    pricing: {
      operations: {
        completion: {
          measures: { tokens: { unit: "token" } },
          dimensions: {},
        },
      },
      rate_cards: {
        standard: {
          operations: {
            completion: {
              rules: [],
              unmatched: {
                action: "charge",
                charge: { type: "expression", formula: "tokens" },
              },
            },
          },
        },
      },
    },
    credits: {
      buckets: { purchased: { priority: 1, expiry: { type: "never" } } },
      default_bucket: "purchased",
    },
    plans: {
      pro: {
        display_name: displayName,
        rank: 0,
        rate_card: "standard",
        allowed_operations: ["completion"],
      },
    },
    commerce: {
      providers: { stripe: { type: "stripe" } },
      offers: {
        tenant_isolation_topup: {
          type: "topup",
          display_name: "Tenant isolation top-up",
          price: { amount_minor: 1000, currency: "USD" },
          providers: {
            stripe: { type: "stripe_price", price_id: REFUND_TOPUP_PRICE_ID },
          },
          credits_per_unit: "1000",
          bucket: "purchased",
          quantity: { minimum: 1, maximum: 10, default: 1 },
        },
      },
    },
  };
}

describe.runIf(DATABASE_URL)("configuration catalog RLS", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
  beforeAll(async () => {
    await applyMigrations(pool);
    await truncateBursarTables(pool);
    await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
      SECOND_TENANT_ID,
      "security-second",
      "Security second tenant",
    ]);
  }, 60_000);
  afterAll(async () => {
    await pool.end();
  });

  it("scopes catalog RPC state by tenant and denies private table access", async () => {
    const firstStore = new PostgresStore({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    const secondStore = new PostgresStore({
      postgres: pool,
      tenantId: SECOND_TENANT_ID,
      providerEnvironment: "test",
    });

    let firstRevisionId: string;
    let secondRevisionId: string;
    try {
      firstRevisionId = await firstStore.publishAndActivateCatalog(
        catalogConfig("First tenant Pro"),
        "first-tenant",
      );
      secondRevisionId = await secondStore.publishAndActivateCatalog(
        catalogConfig("Second tenant Pro"),
        "second-tenant",
      );

      expect(firstRevisionId).not.toBe(secondRevisionId);
      await expect(firstStore.getActiveCatalog()).resolves.toMatchObject({
        id: firstRevisionId,
        version: 1,
        config: { plans: { pro: { display_name: "First tenant Pro" } } },
      });
      await expect(secondStore.getActiveCatalog()).resolves.toMatchObject({
        id: secondRevisionId,
        version: 1,
        config: { plans: { pro: { display_name: "Second tenant Pro" } } },
      });
    } finally {
      await Promise.all([firstStore.close(), secondStore.close()]);
    }

    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SET LOCAL ROLE bursar_client");
      await client.query("SELECT set_config('bursar.provider_environment', 'test', true)");

      const activeCatalog = async (tenantId: string) => {
        await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [tenantId]);
        return client.query<{
          tenant_id: string;
          id: string;
          revision_no: string;
          status: string;
          label: string;
          display_name: string;
        }>(`SELECT
              (revision).tenant_id::text AS tenant_id,
              (revision).id::text AS id,
              (revision).revision_no::text AS revision_no,
              (revision).status::text AS status,
              (revision).label AS label,
              (revision).source_document #>> '{plans,pro,display_name}' AS display_name
            FROM (
              SELECT bursar.active_catalog_revision() AS revision
            ) AS active`);
      };

      const firstActive = await activeCatalog(TEST_TENANT_ID);
      expect(firstActive.rows).toEqual([
        {
          tenant_id: TEST_TENANT_ID,
          id: firstRevisionId,
          revision_no: "1",
          status: "active",
          label: "first-tenant",
          display_name: "First tenant Pro",
        },
      ]);

      const secondActive = await activeCatalog(SECOND_TENANT_ID);
      expect(secondActive.rows).toEqual([
        {
          tenant_id: SECOND_TENANT_ID,
          id: secondRevisionId,
          revision_no: "1",
          status: "active",
          label: "second-tenant",
          display_name: "Second tenant Pro",
        },
      ]);

      await expect(
        client.query("SELECT count(*) FROM bursar.catalog_revisions"),
      ).rejects.toMatchObject({
        code: "42501",
      });
    } finally {
      await client.query("ROLLBACK").catch(() => undefined);
      client.release();
    }
  });

  it("isolates same-subject idempotency keys, balances, and ledgers across tenants", async () => {
    const firstStore = new PostgresStore({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    const secondStore = new PostgresStore({
      postgres: pool,
      tenantId: SECOND_TENANT_ID,
      providerEnvironment: "test",
    });

    try {
      // Keep this test runnable on its own when Vitest filters out the catalog test above.
      if ((await firstStore.getActiveCatalog()) === null) {
        await firstStore.publishAndActivateCatalog(
          catalogConfig("First tenant Pro"),
          "tenant-isolation-first",
        );
      }
      if ((await secondStore.getActiveCatalog()) === null) {
        await secondStore.publishAndActivateCatalog(
          catalogConfig("Second tenant Pro"),
          "tenant-isolation-second",
        );
      }

      const firstGrant = await firstStore.addCredits(
        TENANT_ISOLATION_SUBJECT_ID,
        new Decimal("10"),
        { idempotencyKey: "same-subject-same-key" },
      );
      const secondGrant = await secondStore.addCredits(
        TENANT_ISOLATION_SUBJECT_ID,
        new Decimal("20"),
        { idempotencyKey: "same-subject-same-key" },
      );
      expect(firstGrant.newBalance.toString()).toBe("10");
      expect(secondGrant.newBalance.toString()).toBe("20");

      const firstReplay = await firstStore.addCredits(
        TENANT_ISOLATION_SUBJECT_ID,
        new Decimal("10"),
        { idempotencyKey: "same-subject-same-key" },
      );
      const secondReplay = await secondStore.addCredits(
        TENANT_ISOLATION_SUBJECT_ID,
        new Decimal("20"),
        { idempotencyKey: "same-subject-same-key" },
      );
      expect(firstReplay.idempotent).toBe(true);
      expect(secondReplay.idempotent).toBe(true);
      expect((await firstStore.getBalance(TENANT_ISOLATION_SUBJECT_ID)).balance.toString()).toBe(
        "10",
      );
      expect((await secondStore.getBalance(TENANT_ISOLATION_SUBJECT_ID)).balance.toString()).toBe(
        "20",
      );

      const firstLedger = await firstStore.listLedgerEntries(TENANT_ISOLATION_SUBJECT_ID, {
        limit: 10,
      });
      const secondLedger = await secondStore.listLedgerEntries(TENANT_ISOLATION_SUBJECT_ID, {
        limit: 10,
      });
      expect(firstLedger.items).toHaveLength(1);
      expect(secondLedger.items).toHaveLength(1);
      expect(firstLedger.items[0]?.amount.toString()).toBe("10");
      expect(secondLedger.items[0]?.amount.toString()).toBe("20");
    } finally {
      await Promise.all([firstStore.close(), secondStore.close()]);
    }
  });

  it("isolates provider refund identities and payment references across tenants", async () => {
    const firstCreditStore = new PostgresStore({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    const secondCreditStore = new PostgresStore({
      postgres: pool,
      tenantId: SECOND_TENANT_ID,
      providerEnvironment: "test",
    });
    const firstBillingStore = new PostgresBillingStore({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    const secondBillingStore = new PostgresBillingStore({
      postgres: pool,
      tenantId: SECOND_TENANT_ID,
      providerEnvironment: "test",
    });
    const firstCredits = new CreditsService(firstCreditStore);
    const secondCredits = new CreditsService(secondCreditStore);
    const firstBilling = new BillingService(firstBillingStore, { provisioning: firstCredits });
    const secondBilling = new BillingService(secondBillingStore, { provisioning: secondCredits });

    const paymentEvent = (eventId: string, providerPaymentId: string) => ({
      provider: REFUND_PROVIDER,
      eventId,
      eventType: "payment.succeeded" as const,
      occurredAt: "2026-08-19T10:00:00.000Z",
      accountId: REFUND_ISOLATION_SUBJECT_ID,
      payment: {
        providerPaymentId,
        amountMinor: 1000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "tenant-isolation-topup", priceId: REFUND_TOPUP_PRICE_ID },
        purpose: "credit_topup" as const,
        status: "succeeded" as const,
      },
    });
    const refundEvent = (
      eventId: string,
      providerPaymentId: string,
      providerRefundId: string,
      amountMinor: number,
    ) => ({
      provider: REFUND_PROVIDER,
      eventId,
      eventType: "refund.created" as const,
      occurredAt: "2026-08-19T10:01:00.000Z",
      accountId: REFUND_ISOLATION_SUBJECT_ID,
      refund: {
        providerRefundId,
        providerPaymentId,
        amountMinor,
        currency: "USD",
        status: "succeeded" as const,
      },
    });

    try {
      if ((await firstCreditStore.getActiveCatalog()) === null) {
        await firstCredits.publishAndActivateCatalog(
          catalogConfig("First tenant Pro"),
          "refund-isolation-first",
        );
      }
      if ((await secondCreditStore.getActiveCatalog()) === null) {
        await secondCredits.publishAndActivateCatalog(
          catalogConfig("Second tenant Pro"),
          "refund-isolation-second",
        );
      }

      const sharedPaymentId = "payment_tenant_refund_shared";
      const sharedRefundId = "refund_tenant_identity_shared";
      await expect(
        firstBilling.ingestBillingEvent(paymentEvent("evt_tenant_payment_shared", sharedPaymentId)),
      ).resolves.toMatchObject({ handled: true });
      await expect(
        secondBilling.ingestBillingEvent(
          paymentEvent("evt_tenant_payment_shared", sharedPaymentId),
        ),
      ).resolves.toMatchObject({ handled: true });

      const secondOnlyPaymentId = "payment_tenant_refund_second_only";
      await expect(
        secondBilling.ingestBillingEvent(
          paymentEvent("evt_tenant_payment_second_only", secondOnlyPaymentId),
        ),
      ).resolves.toMatchObject({ handled: true });

      await expect(
        firstBilling.ingestBillingEvent(
          refundEvent("evt_tenant_refund_shared", sharedPaymentId, sharedRefundId, 500),
        ),
      ).resolves.toMatchObject({ handled: true, action: "refund_clawback" });
      await expect(
        secondBilling.ingestBillingEvent(
          refundEvent("evt_tenant_refund_shared", sharedPaymentId, sharedRefundId, 1000),
        ),
      ).resolves.toMatchObject({ handled: true, action: "refund_clawback" });

      expect((await firstCredits.getBalance(REFUND_ISOLATION_SUBJECT_ID)).balance.toString()).toBe(
        "500",
      );
      expect((await secondCredits.getBalance(REFUND_ISOLATION_SUBJECT_ID)).balance.toString()).toBe(
        "1000",
      );

      const isolatedRows = await pool.query<{
        allocation_count: number;
        clawback_count: number;
        credit_amount: string;
        refund_count: number;
        tenant_id: string;
      }>(
        `SELECT refund.tenant_id::text AS tenant_id,
                COUNT(DISTINCT refund.id)::int AS refund_count,
                COUNT(allocation.refund_id)::int AS allocation_count,
                COUNT(ledger.id)::int AS clawback_count,
                SUM(allocation.credit_amount)::text AS credit_amount
         FROM bursar.billing_refunds AS refund
         LEFT JOIN bursar.billing_refund_grants AS allocation
           ON allocation.refund_id = refund.id
          AND allocation.tenant_id = refund.tenant_id
         LEFT JOIN bursar.credit_ledger_entries AS ledger
           ON ledger.id = allocation.ledger_entry_id
          AND ledger.tenant_id = refund.tenant_id
         WHERE refund.provider = $1
           AND refund.provider_refund_id = $2
         GROUP BY refund.tenant_id
         ORDER BY refund.tenant_id`,
        [REFUND_PROVIDER, sharedRefundId],
      );
      expect(isolatedRows.rows).toEqual([
        {
          tenant_id: TEST_TENANT_ID,
          refund_count: 1,
          allocation_count: 1,
          clawback_count: 1,
          credit_amount: "500.000000",
        },
        {
          tenant_id: SECOND_TENANT_ID,
          refund_count: 1,
          allocation_count: 1,
          clawback_count: 1,
          credit_amount: "1000.000000",
        },
      ]);

      const crossTenant = await firstBilling.ingestBillingEvent(
        refundEvent(
          "evt_tenant_refund_cross_reference",
          secondOnlyPaymentId,
          "refund_tenant_cross_reference",
          1000,
        ),
      );
      expect(crossTenant.handled).toBe(false);
      expect(crossTenant.error).toBeTruthy();
      expect((await firstCredits.getBalance(REFUND_ISOLATION_SUBJECT_ID)).balance.toString()).toBe(
        "500",
      );
      expect((await secondCredits.getBalance(REFUND_ISOLATION_SUBJECT_ID)).balance.toString()).toBe(
        "1000",
      );
      const crossRows = await pool.query<{ count: number }>(
        `SELECT COUNT(*)::int AS count
         FROM bursar.billing_refunds
         WHERE tenant_id = $1::uuid
           AND provider = $2
           AND provider_refund_id = $3`,
        [TEST_TENANT_ID, REFUND_PROVIDER, "refund_tenant_cross_reference"],
      );
      expect(crossRows.rows[0]?.count).toBe(0);
    } finally {
      await Promise.all([
        firstCreditStore.close(),
        secondCreditStore.close(),
        firstBillingStore.close(),
        secondBillingStore.close(),
      ]);
    }
  });

  it("rejects mutations for a suspended tenant without affecting its peer", async () => {
    const firstStore = new PostgresStore({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    const secondStore = new PostgresStore({
      postgres: pool,
      tenantId: SECOND_TENANT_ID,
      providerEnvironment: "test",
    });
    let suspended = false;

    try {
      if ((await firstStore.getActiveCatalog()) === null) {
        await firstStore.publishAndActivateCatalog(
          catalogConfig("First tenant Pro"),
          "suspended-first",
        );
      }
      if ((await secondStore.getActiveCatalog()) === null) {
        await secondStore.publishAndActivateCatalog(
          catalogConfig("Second tenant Pro"),
          "suspended-second",
        );
      }
      await firstStore.addCredits(SUSPENDED_TENANT_SUBJECT_ID, new Decimal("3"), {
        idempotencyKey: "suspended-peer-first",
      });
      await secondStore.addCredits(SUSPENDED_TENANT_SUBJECT_ID, new Decimal("7"), {
        idempotencyKey: "suspended-peer-second",
      });

      await pool.query("SELECT bursar.set_tenant_status($1::uuid, 'suspended')", [
        SECOND_TENANT_ID,
      ]);
      suspended = true;
      await expect(
        secondStore.addCredits(SUSPENDED_TENANT_SUBJECT_ID, new Decimal("1"), {
          idempotencyKey: "suspended-mutation",
        }),
      ).rejects.toThrow();

      expect((await firstStore.getBalance(SUSPENDED_TENANT_SUBJECT_ID)).balance.toString()).toBe(
        "3",
      );
      await pool.query("SELECT bursar.set_tenant_status($1::uuid, 'active')", [SECOND_TENANT_ID]);
      suspended = false;
      expect((await secondStore.getBalance(SUSPENDED_TENANT_SUBJECT_ID)).balance.toString()).toBe(
        "7",
      );
    } finally {
      if (suspended) {
        await pool.query("SELECT bursar.set_tenant_status($1::uuid, 'active')", [SECOND_TENANT_ID]);
      }
      await Promise.all([firstStore.close(), secondStore.close()]);
    }
  });

  it("fails closed when a restricted RPC has no tenant context", async () => {
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SET LOCAL ROLE bursar_client");
      await client.query("SELECT set_config('bursar.provider_environment', 'test', true)");
      await expect(
        client.query(
          `SELECT * FROM bursar.post_credit(
            $1,
            'adjustment',
            1,
            'tenant-context-test-js',
            'tenant-context-test-js'
          )`,
          [TENANT_ISOLATION_SUBJECT_ID],
        ),
      ).rejects.toMatchObject({ code: "28000" });
    } finally {
      await client.query("ROLLBACK").catch(() => undefined);
      client.release();
    }
  });
});
