import { describe, expect, it, vi } from "vitest";
import { Bursar } from "../src/bursar.js";
import type { BillingStore } from "../src/billing/billing-store.js";
import type { CreditStore } from "../src/credits/store.js";

describe("Bursar facade", () => {
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
});
