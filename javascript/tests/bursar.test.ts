import { describe, expect, it, vi } from "vitest";
import { AccountService, Bursar } from "../src/bursar.js";
import type { BillingStore } from "../src/billing/billing-store.js";
import type { CreditStore } from "../src/credits/store.js";
import { CommerceNotConfiguredError, ConfigError, PricingNotLoadedError } from "../src/index.js";

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
    const accounts = new AccountService(credits as never, catalog as never);

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

  it("owns one credit service and exposes catalog operations", async () => {
    const creditStore = {} as CreditStore;
    const bursar = new Bursar({ creditStore });

    expect(bursar.billing).toBeNull();
    expect(bursar.catalog).toBeDefined();
    const active = { version: 3 };
    vi.spyOn(bursar.credits, "getActivePricing").mockReturnValue(active as never);
    expect(bursar.catalog.active).toBe(active);
  });

  it("configures billing with the facade-owned credit provisioning capability", () => {
    const credits = {} as ConstructorParameters<typeof Bursar>[0]["credits"];
    const billingStore = {} as BillingStore;
    const bursar = new Bursar({ creditStore: {} as CreditStore, billingStore, credits });

    expect(bursar.billing).not.toBeNull();
    expect(bursar.billing?.hasProvisioning).toBe(true);
  });

  it("routes provider events through the facade-owned billing service", async () => {
    const bursar = new Bursar({ creditStore: {} as CreditStore, billingStore: {} as BillingStore });
    const ingest = vi
      .spyOn(bursar.billing!, "ingestBillingEvent")
      .mockResolvedValue({ handled: true, action: "subscription_created" });
    const event = {
      provider: "mock",
      eventId: "evt-1",
      eventType: "subscription.created",
    } as never;

    await expect(bursar.ingestBillingEvent(event)).resolves.toEqual({
      handled: true,
      action: "subscription_created",
    });
    expect(ingest).toHaveBeenCalledWith(event);
  });

  it("emits typed errors from public facade operations", async () => {
    const bursar = new Bursar({ creditStore: {} as CreditStore });
    vi.spyOn(bursar.credits, "getActivePricing").mockReturnValue(null);

    await expect(bursar.catalog.getConfig()).rejects.toBeInstanceOf(PricingNotLoadedError);
    await expect(bursar.ingestBillingEvent({} as never)).rejects.toBeInstanceOf(
      CommerceNotConfiguredError,
    );

    const accounts = new AccountService(
      {
        getUserPlan: vi.fn(),
        setUserPlan: vi.fn(),
        executeGrantProgram: vi.fn(),
      } as never,
      {
        getConfig: vi.fn().mockResolvedValue({
          catalog: {},
          plans: {},
          credits: { grantPrograms: {} },
        }),
      } as never,
    );
    await expect(
      accounts.onAccountCreated({ accountId: "user-1", eventKey: "signup:user-1" }),
    ).rejects.toBeInstanceOf(ConfigError);
  });
});
