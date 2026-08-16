import { describe, expect, it, vi } from "vitest";
import {
  AccountService,
  Bursar,
  type BursarOptions,
  type CatalogService,
  type CreditsService,
} from "../src/bursar.js";
import type { BillingStore } from "../src/billing/billing-store.js";
import type { BillingEvent } from "../src/billing/types/index.js";
import type { CreditStore } from "../src/credits/store.js";
import {
  CapabilityNotConfiguredError,
  CommerceNotConfiguredError,
  ConfigError,
  CatalogNotLoadedError,
} from "../src/index.js";

function testCredits(value: Partial<CreditsService>): CreditsService {
  // SAFETY: Each fixture implements the CreditsService methods used by its scenario.
  return value as CreditsService;
}

function testCatalog(value: Partial<CatalogService>): CatalogService {
  // SAFETY: Each fixture implements the CatalogService methods used by its scenario.
  return value as CatalogService;
}

function testCreditStore(value: Partial<CreditStore>): CreditStore {
  // SAFETY: Each fixture implements the CreditStore methods used by its scenario.
  return value as CreditStore;
}

function testBillingStore(value: Partial<BillingStore>): BillingStore {
  // SAFETY: Each fixture implements the BillingStore methods used by its scenario.
  return value as BillingStore;
}

describe("Bursar facade", () => {
  it("initializes a default plan and all account-created grants idempotently", async () => {
    const credits = {
      getUserPlan: vi.fn().mockResolvedValue({ planKey: null }),
      setUserPlan: vi.fn().mockResolvedValue(undefined),
      executeGrantProgram: vi.fn().mockResolvedValue([{ replayed: false }]),
    };
    const catalog = {
      getConfig: vi.fn().mockResolvedValue({
        catalog: { defaultPlan: "free" },
        plans: { free: { rank: 0 } },
        credits: {
          grantPrograms: {
            referral: { trigger: "referral_completed" },
            welcome: { trigger: "account_created" },
          },
        },
      }),
    };
    const accounts = new AccountService(testCredits(credits), testCatalog(catalog));

    const result = await accounts.onAccountCreated({
      accountId: "user-1",
      eventKey: "signup:user-1",
    });

    expect(result).toMatchObject({ planKey: "free", planAssigned: true });
    expect(credits.setUserPlan).toHaveBeenCalledWith("user-1", "free");
    expect(credits.executeGrantProgram).toHaveBeenCalledTimes(1);
    expect(credits.executeGrantProgram).toHaveBeenCalledWith(
      expect.objectContaining({
        programKey: "welcome",
        eventKey: "signup:user-1",
      }),
    );
  });

  it("validates account lifecycle identity before catalog access", async () => {
    const catalog = { getConfig: vi.fn() };
    const accounts = new AccountService(testCredits({}), testCatalog(catalog));

    await expect(
      accounts.onAccountCreated({ accountId: " ", eventKey: "signup:1" }),
    ).rejects.toThrow(/accountId/);
    expect(catalog.getConfig).not.toHaveBeenCalled();
  });

  it("owns one credit service and exposes catalog operations", async () => {
    const active = { version: 3 };
    const getActiveCatalog = vi.fn().mockResolvedValue(active);
    const applyDuePlanChanges = vi.fn().mockResolvedValue(2);
    const creditStore = testCreditStore({ getActiveCatalog, applyDuePlanChanges });
    const bursar = new Bursar({ creditStore });

    expect(bursar.billing).toBeNull();
    expect(bursar.catalog).toBeDefined();
    await expect(bursar.catalog.getActive()).resolves.toBe(active);
    await expect(bursar.catalog.applyDueChanges(25)).resolves.toBe(2);
    expect(applyDuePlanChanges).toHaveBeenCalledWith(25);
  });

  it("rejects billing and commerce options that cannot be applied", () => {
    const creditStore = testCreditStore({});

    // SAFETY: This intentionally invalid option shape exercises Bursar's runtime validation.
    expect(() => new Bursar({ creditStore, billingOptions: {} } as BursarOptions)).toThrow(
      /require billingStore/,
    );
    expect(
      () =>
        // SAFETY: This intentionally invalid option shape exercises Bursar's runtime validation.
        new Bursar({
          creditStore,
          commerceOptions: { providerEnvironment: "test", providers: {} },
        } as BursarOptions),
    ).toThrow(/require billingStore/);
  });

  it("configures billing with the facade-owned credit provisioning capability", () => {
    const billingStore = testBillingStore({});
    const bursar = new Bursar({ creditStore: testCreditStore({}), billingStore });

    expect(bursar.billing).not.toBeNull();
    expect(bursar.billing?.hasProvisioning).toBe(true);
  });

  it("rejects mismatched credit and billing provider environments", () => {
    const creditStore = testCreditStore({
      providerEnvironment: "live",
    });
    const billingStore = testBillingStore({
      providerEnvironment: "test",
    });

    expect(
      () =>
        new Bursar({
          creditStore,
          billingStore,
          commerceOptions: { providerEnvironment: "test", providers: {} },
        }),
    ).toThrow(/must match/);
  });

  it("routes provider events through the facade-owned billing service", async () => {
    const bursar = new Bursar({
      creditStore: testCreditStore({}),
      billingStore: testBillingStore({}),
    });
    const ingest = vi
      .spyOn(bursar.billing!, "ingestBillingEvent")
      .mockResolvedValue({ handled: true, action: "subscription_created" });
    const event: BillingEvent = {
      provider: "mock",
      eventId: "evt-1",
      eventType: "subscription.created",
      occurredAt: "2026-08-05T00:00:00Z",
    };

    await expect(bursar.ingestBillingEvent(event)).resolves.toEqual({
      handled: true,
      action: "subscription_created",
    });
    expect(ingest).toHaveBeenCalledWith(event);
  });

  it("emits typed errors from public facade operations", async () => {
    const creditStore = testCreditStore({
      getActiveCatalog: vi.fn().mockResolvedValue(null),
    });
    const bursar = new Bursar({ creditStore });

    await expect(bursar.catalog.getConfig()).rejects.toBeInstanceOf(CatalogNotLoadedError);
    await expect(
      bursar.ingestBillingEvent({
        provider: "mock",
        eventId: "evt-no-billing",
        eventType: "customer.created",
        occurredAt: "2026-08-05T00:00:00Z",
      }),
    ).rejects.toBeInstanceOf(CapabilityNotConfiguredError);
    expect(() => bursar.requireBilling()).toThrow(CapabilityNotConfiguredError);
    expect(() => bursar.requireCommerce()).toThrow(CommerceNotConfiguredError);

    const accounts = new AccountService(
      testCredits({
        getUserPlan: vi.fn(),
        setUserPlan: vi.fn(),
        executeGrantProgram: vi.fn(),
      }),
      testCatalog({
        getConfig: vi.fn().mockResolvedValue({
          catalog: {},
          plans: { free: { rank: 0 } },
          credits: { grantPrograms: {} },
        }),
      }),
    );
    await expect(
      accounts.onAccountCreated({ accountId: "user-1", eventKey: "signup:user-1" }),
    ).rejects.toBeInstanceOf(ConfigError);
  });
});
