import { describe, expect, it } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { canonicalBursarConfigDict } from "../src/config.js";
import { loadConfigFile, loadRawConfigFile } from "../src/load-config-file.js";
import { normalizeConfig } from "../src/config/normalize.js";
import { PricingEngine } from "../src/engine.js";

const starter = {
  version: 1,
  catalog: { default_plan: "free" },
  credits: { on_signup: "20" },
  pricing: { operations: { export_report: { charge: { type: "flat", amount: "2" } } } },
  plans: { free: { display_name: "Free", allowed_operations: ["export_report"] } },
};

describe("config authoring", () => {
  it("compiles the starter to a replay-safe durable catalog", () => {
    const result = canonicalBursarConfigDict(starter);
    expect(result.version).toBe(1);
    expect(result.plans?.free?.allowed_operations).toEqual(["export_report"]);
    expect(result.plans?.free?.tier).toBe(0);
    expect(result.credits!.grant_programs?.signup?.idempotency_scope).toBe("subject");
    expect(result.pricing?.operations.export_report?.measures).toEqual({});
  });
  it("does not use display rank as the upgrade tier", () => {
    const source = { ...starter, plans: { free: { ...starter.plans.free, rank: 8 } } };
    expect(canonicalBursarConfigDict(source).plans?.free?.tier).toBe(0);
  });
  it.each(["all", "none"] as const)("roundtrips scalar access %s", (access) => {
    const source = {
      ...starter,
      plans: { free: { display_name: "Free", allowed_operations: access } },
    };
    const canonical = canonicalBursarConfigDict(source);
    expect(canonical.plans?.free?.allowed_operations).toBe(access);
    expect(canonical.plans?.free).not.toHaveProperty("operation_access");
    expect(canonicalBursarConfigDict(canonical)).toEqual(canonical);
  });
  it("rejects empty access lists and internal markers", () => {
    expect(() =>
      canonicalBursarConfigDict({
        ...starter,
        plans: { free: { display_name: "Free", allowed_operations: [] } },
      }),
    ).toThrow();
    expect(() =>
      canonicalBursarConfigDict({
        ...starter,
        plans: { free: { display_name: "Free", operation_access: "all" } },
      }),
    ).toThrow();
  });
  it("requires pricing when all operations are allowed", () => {
    const { pricing: _pricing, ...withoutPricing } = starter;
    const source = {
      ...withoutPricing,
      plans: { free: { display_name: "Free", allowed_operations: "all" } },
    };
    expect(() => canonicalBursarConfigDict(source)).toThrow(
      /plans\.free\.allowed_operations.*pricing/,
    );
    expect(() =>
      canonicalBursarConfigDict({ ...source, plans: { free: { display_name: "Free" } } }),
    ).not.toThrow();
  });
  it("requires authored metered measures unless explicitly optional", () => {
    const operation = {
      measures: { items: { unit: "item" } },
      charge: { type: "per_unit", measure: "items", rate: "2" },
    };
    const source = { ...starter, pricing: { operations: { export_report: operation } } };
    const engine = PricingEngine.fromDict(source);
    expect(() => engine.calculate({ operation: "export_report" })).toThrow(
      /requires usage measure/,
    );
    expect(
      engine.calculate({ operation: "export_report", measures: { items: 0 } }).total.isZero(),
    ).toBe(true);
    const optional = {
      ...source,
      pricing: {
        operations: {
          export_report: { ...operation, measures: { items: { unit: "item", required: false } } },
        },
      },
    };
    expect(
      PricingEngine.fromDict(optional).calculate({ operation: "export_report" }).total.isZero(),
    ).toBe(true);
  });
});

describe("provider environment bindings", () => {
  it("keeps authored bindings and resolves only at use", async () => {
    const { resolveProviderId } = await import("../src/config/parse-commerce.js");
    const mapping = { test: "price_test", live: "price_live" };
    expect(resolveProviderId(mapping, "test")).toBe("price_test");
    expect(resolveProviderId(mapping, "live")).toBe("price_live");
    expect(() => resolveProviderId(mapping, "sandbox")).toThrow(/sandbox/);
    const fixture = await loadRawConfigFile("../tests/parity/provider_environment_bindings.json");
    expect(normalizeConfig({ ...starter, ...fixture }, "test").commerce).toEqual(fixture.commerce);
  });
});

describe("durable provider bindings", () => {
  const config = {
    ...starter,
    commerce: {
      providers: { stripe: { type: "stripe" } },
      offers: {
        monthly: {
          type: "subscription",
          display_name: "Monthly",
          plan: "free",
          billing_interval: { unit: "month" },
          price: { amount_minor: 100, currency: "USD" },
          providers: { stripe: { price_id: { test: "price_test", live: "price_live" } } },
        },
        pack: {
          type: "topup",
          display_name: "Pack",
          credits_per_unit: "10",
          bucket: "default",
          price: { amount_minor: 100, currency: "USD" },
          providers: { stripe: { price_id: { test: "pack_test", live: "pack_live" } } },
        },
      },
    },
  };
  it("preserves mapped IDs and scalar provenance through the Node file loader", async () => {
    const directory = mkdtempSync(join(tmpdir(), "bursar-bindings-"));
    const path = join(directory, "catalog.json");
    try {
      writeFileSync(path, JSON.stringify(config));
      const fromTest = await loadConfigFile(path, "test");
      expect(canonicalBursarConfigDict(fromTest, "live")).toEqual(fromTest);
      expect(fromTest.commerce?.offers?.monthly?.providers.stripe).toMatchObject({
        price_id: { test: "price_test", live: "price_live" },
      });
      const scalar = structuredClone(config);
      Object.assign(scalar.commerce.offers.monthly.providers.stripe, { price_id: "test_only" });
      writeFileSync(path, JSON.stringify(scalar));
      const testOnly = await loadConfigFile(path, "test");
      expect(
        canonicalBursarConfigDict(testOnly, "live").commerce?.offers?.monthly?.providers.stripe,
      ).toMatchObject({ price_id: { test: "test_only" } });
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
  it("retains every mapping through compilation in either namespace", () => {
    const first = canonicalBursarConfigDict(config, "test");
    const second = canonicalBursarConfigDict(first, "live");
    expect(second).toEqual(first);
    expect(second.commerce?.offers?.monthly?.providers.stripe).toMatchObject({
      price_id: { test: "price_test", live: "price_live" },
    });
    expect(
      PricingEngine.fromDict(config).calculate({ operation: "export_report" }).total.toString(),
    ).toBe("2");
  });
  it("scopes scalar IDs without later reinterpreting them", async () => {
    const { resolveProviderId } = await import("../src/config/parse-commerce.js");
    const source = structuredClone(config);
    Object.assign(source.commerce.offers.monthly.providers.stripe, { price_id: "test_only" });
    const first = canonicalBursarConfigDict(source, "test");
    const second = canonicalBursarConfigDict(first, "live");
    expect(second.commerce?.offers?.monthly?.providers.stripe).toMatchObject({
      price_id: { test: "test_only" },
    });
    expect(() => resolveProviderId({ test: "test_only" }, "live")).toThrow(/live/);
  });
  it("checks duplicate provider IDs in all environments", () => {
    const source = structuredClone(config);
    source.commerce.offers.pack.providers.stripe.price_id.live = "price_live";
    expect(() => canonicalBursarConfigDict(source, "test")).toThrow(/duplicate provider/);
  });
});
