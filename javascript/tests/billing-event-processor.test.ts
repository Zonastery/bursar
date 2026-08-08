import { describe, expect, it, vi } from "vitest";
import type { BillingStore } from "../src/billing/billing-store.js";
import {
  boundedDiagnosticMessage,
  optionalBoundedDiagnosticMessage,
} from "../src/shared/diagnostics.js";
import { BillingEventHandlers } from "../src/billing/event-handlers.js";
import { BillingEventProcessor } from "../src/billing/event-processor.js";
import { BillingEventRepository } from "../src/billing/postgres/repositories/event.js";
import type { BillingEvent } from "../src/billing/types/index.js";
import { BillingEventType } from "../src/billing/types/index.js";
import { StoreError } from "../src/errors.js";

const CLAIM_TOKEN = "00000000-0000-0000-0000-000000000003";
const BILLING_EVENT_ID = "00000000-0000-0000-0000-000000000004";

function claimedStore() {
  return {
    claimBillingEvent: vi.fn().mockResolvedValue({
      status: "claimed",
      claimToken: CLAIM_TOKEN,
      billingEventId: BILLING_EVENT_ID,
    }),
    completeBillingEvent: vi.fn().mockResolvedValue(true),
    failBillingEvent: vi.fn().mockResolvedValue(true),
    upsertBillingCustomer: vi.fn().mockResolvedValue(undefined),
  };
}

function event(eventId: string, eventType: BillingEventType): BillingEvent {
  return {
    provider: "stripe",
    eventId,
    eventType,
    occurredAt: "2026-08-05T00:00:00Z",
  };
}

describe("BillingEventProcessor lifecycle acknowledgements", () => {
  it.each([
    ["unknown top-level fields", { unexpected: true }],
    ["invalid nested statuses", { payment: { status: "mystery" } }],
    ["coerced nested booleans", { subscription: { cancelAtPeriodEnd: "false" } }],
  ])("rejects %s before claiming the event", async (_description, invalid) => {
    const store = claimedStore();
    const processor = new BillingEventProcessor(store as unknown as BillingStore);
    const changes = invalid as Record<string, unknown>;
    const payment = {
      providerPaymentId: "pay_1",
      amountMinor: 100,
      taxMinor: 0,
      currency: "USD",
      purpose: "credit_topup",
      status: "succeeded",
      ...(changes.payment as object | undefined),
    };
    const subscription = {
      providerSubscriptionId: "sub_1",
      ...(changes.subscription as object | undefined),
    };

    await expect(
      processor.ingestBillingEvent({
        ...event("evt_invalid", BillingEventType.PAYMENT_SUCCEEDED),
        payment,
        subscription,
        ...(changes.unexpected === true ? { unexpected: true } : {}),
      } as unknown as BillingEvent),
    ).rejects.toThrow(TypeError);
    expect(store.claimBillingEvent).not.toHaveBeenCalled();
  });

  it("reports and requeues a rejected completion", async () => {
    const store = claimedStore();
    store.completeBillingEvent.mockResolvedValue(false);
    const processor = new BillingEventProcessor(store as unknown as BillingStore);

    const result = await processor.ingestBillingEvent({
      ...event("evt_completion_rejected", BillingEventType.INVOICE_UPCOMING),
      invoice: {
        providerInvoiceId: "invoice-upcoming",
        status: "draft",
        amountPaidMinor: 0,
        amountDueMinor: 1000,
        currency: "USD",
      },
    });

    expect(result).toEqual({ handled: false, error: "billing_event_completion_rejected" });
    expect(store.failBillingEvent).toHaveBeenCalledWith(
      "stripe",
      "evt_completion_rejected",
      CLAIM_TOKEN,
      "billing_event_completion_rejected",
    );
  });

  it("fails an unhandled event instead of completing it", async () => {
    const store = claimedStore();
    const processor = new BillingEventProcessor(store as unknown as BillingStore);

    const result = await processor.ingestBillingEvent({
      ...event("evt_unhandled", BillingEventType.INVOICE_CREATED),
      invoice: {
        providerInvoiceId: "invoice-created",
        status: "draft",
        amountPaidMinor: 0,
        amountDueMinor: 1000,
        currency: "USD",
      },
    });

    expect(result).toEqual({ handled: false, error: "unhandled_event_type" });
    expect(store.completeBillingEvent).not.toHaveBeenCalled();
    expect(store.failBillingEvent).toHaveBeenCalledWith(
      "stripe",
      "evt_unhandled",
      CLAIM_TOKEN,
      "unhandled_event_type",
    );
  });

  it.each([
    ["   ", "Error"],
    [`  ${"x".repeat(9_000)}  `, "x".repeat(8_192)],
  ])("normalizes processing error %j before persistence", async (rawMessage, expected) => {
    const store = claimedStore();
    store.upsertBillingCustomer.mockRejectedValue(new Error(rawMessage));
    const processor = new BillingEventProcessor(store as unknown as BillingStore);

    const result = await processor.ingestBillingEvent({
      ...event("evt_failure_message", BillingEventType.CUSTOMER_CREATED),
      userId: "00000000-0000-0000-0000-000000000001",
      customer: { providerCustomerId: "cus_failure" },
    });

    expect(result).toEqual({ handled: false, error: expected });
    expect(store.failBillingEvent).toHaveBeenCalledWith(
      "stripe",
      "evt_failure_message",
      CLAIM_TOKEN,
      expected,
    );
  });
});

describe("billing diagnostic and repository boundaries", () => {
  it("preserves absent diagnostics and removes NUL characters", () => {
    expect(optionalBoundedDiagnosticMessage(null)).toBeNull();
    expect(boundedDiagnosticMessage("  failed\0message  ")).toBe("failed\uFFFDmessage");
  });

  it("returns lifecycle RPC acknowledgements", async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce([{ completed: true }])
      .mockResolvedValueOnce([{ failed: false }]);
    const repository = new BillingEventRepository(query);

    await expect(repository.complete("stripe", "evt_repository", CLAIM_TOKEN)).resolves.toBe(true);
    await expect(repository.fail("stripe", "evt_repository", CLAIM_TOKEN, "failed")).resolves.toBe(
      false,
    );
  });

  it("fails closed when a lifecycle mutation returns no result", async () => {
    const repository = new BillingEventRepository(vi.fn().mockResolvedValue([]));

    await expect(
      repository.claim("stripe", "evt_missing", "invoice.paid", "{}"),
    ).rejects.toMatchObject({
      name: StoreError.name,
      indeterminate: true,
    });
    await expect(repository.complete("stripe", "evt_missing", CLAIM_TOKEN)).rejects.toMatchObject({
      name: StoreError.name,
      indeterminate: true,
    });
  });
});

describe("subscription plan-change provisioning", () => {
  it("starts immediately when the existing allowance anchor is unavailable", async () => {
    const setUserPlan = vi.fn().mockResolvedValue(undefined);
    const store = {
      getBillingSubscription: vi.fn().mockResolvedValue({
        userId: "user-1",
        provider: "dodo",
        providerSubscriptionId: "sub-1",
        status: "active",
      }),
      resolveBillingOffer: vi.fn().mockResolvedValue({
        offerId: "offer-sage",
        offerKey: "sage_monthly",
        plan: "sage",
      }),
      getOpenBillingSubscriptionChange: vi.fn().mockResolvedValue(null),
      upsertBillingSubscription: vi.fn().mockResolvedValue(undefined),
    } as unknown as BillingStore;

    const handlers = new BillingEventHandlers(store, {
      autoSelectEntitlementSource: false,
      provisioning: {
        setUserPlan,
        unsetUserPlan: vi.fn(),
        addCredits: vi.fn(),
        deductCredits: vi.fn(),
        revokeCreditsByEntryType: vi.fn(),
      },
    });

    const handler = handlers.getHandler(BillingEventType.SUBSCRIPTION_PLAN_CHANGED);
    await handler?.({
      provider: "dodo",
      eventId: "evt_plan_change",
      eventType: BillingEventType.SUBSCRIPTION_PLAN_CHANGED,
      occurredAt: "2026-08-05T00:00:00Z",
      userId: "user-1",
      subscription: {
        providerSubscriptionId: "sub-1",
        status: "active",
        periodStart: "2030-01-01T00:00:00.000Z",
        refs: { productId: "product-sage" },
      },
    });

    expect(setUserPlan).toHaveBeenCalledTimes(1);
    const assignedAt = setUserPlan.mock.calls[0]?.[2];
    expect(assignedAt).toBeInstanceOf(Date);
    expect((assignedAt as Date).getTime()).toBeLessThanOrEqual(Date.now());
  });
});
