import { describe, expect, it, vi } from "vitest";
import { Bursar } from "../src/bursar.js";
import type { BillingStore } from "../src/billing/billing-store.js";
import type { CreditStore } from "../src/stores/credit-store.js";

describe("Bursar facade", () => {
  it("owns one credit service and exposes catalog operations", async () => {
    const setup = vi.fn().mockResolvedValue({});
    const creditStore = { setup } as unknown as CreditStore;
    const bursar = new Bursar({ creditStore });

    expect(bursar.billing).toBeNull();
    expect(bursar.catalog).toBeDefined();
    await bursar.setup();
    expect(setup).toHaveBeenCalledOnce();

    const active = { version: 3 };
    vi.spyOn(bursar.credits, "getActivePricing").mockReturnValue(active as never);
    expect(bursar.catalog.active).toBe(active);
  });

  it("wires billing provisioning to the facade-owned credit service", () => {
    const credits = {} as ConstructorParameters<typeof Bursar>[0]["credits"];
    const billingStore = {} as BillingStore;
    const bursar = new Bursar({ creditStore: {} as CreditStore, billingStore, credits });

    expect(bursar.billing).not.toBeNull();
    expect((bursar.billing as unknown as { provisioning: unknown }).provisioning).toBe(
      bursar.credits,
    );
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
