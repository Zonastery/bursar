import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import Decimal from "decimal.js";
import pg from "pg";
import { CreditsService } from "../src/credits-service.js";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { BOOTSTRAP_SQL, applyMigrations } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");
const USER_ID = "00000000-0000-0000-0000-000000000902";

const CONFIG = {
  version: 1,
  usage: {
    operations: {
      completion: { measures: ["input_tokens", "output_tokens"], dimensions: ["model"] },
    },
    rate_cards: {
      standard: {
        prices: {
          completion: [
            {
              match: { model: { prefix: "premium-" } },
              formula: "input_tokens * 2 + output_tokens * 3",
            },
            { default: true, formula: "input_tokens + output_tokens" },
          ],
        },
      },
    },
  },
  credits: {
    buckets: { grant: {}, purchased: {} },
    spend_order: ["grant", "purchased"],
    default_bucket: "purchased",
  },
  plans: { pro: { display_name: "Pro", rate_card: "standard" } },
};

describe.runIf(DATABASE_URL)("PostgresStore integration — public configuration", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
  const store = new PostgresStore(DATABASE_URL!, pool);

  beforeAll(async () => {
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    await pool.query("INSERT INTO auth.users (id) VALUES ($1) ON CONFLICT DO NOTHING", [USER_ID]);
    await pool.query('INSERT INTO public."user" (id) VALUES ($1) ON CONFLICT DO NOTHING', [
      USER_ID,
    ]);
  }, 60_000);
  afterAll(async () => pool.end());

  it("publishes the public config, charges a generic operation, and preserves bucket order", async () => {
    const service = new CreditsService(store);
    await service.publishPricingFromDict(CONFIG);
    await service.addCredits(USER_ID, new Decimal(10), "purchase", {}, undefined, "grant");
    await service.addCredits(USER_ID, new Decimal(10), "purchase", {}, undefined, "purchased");

    const result = await service.deduct(
      USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 2, output_tokens: 4 },
        dimensions: { model: "premium-x" },
      },
      "public-config-charge-1",
    );
    expect(result.amount.toString()).toBe("16");
    const buckets = await service.getBucketBalances(USER_ID);
    expect(buckets.buckets.find((bucket) => bucket.bucketKey === "grant")?.balance.toString()).toBe(
      "0",
    );
    expect(
      buckets.buckets.find((bucket) => bucket.bucketKey === "purchased")?.balance.toString(),
    ).toBe("4");
  });
});
