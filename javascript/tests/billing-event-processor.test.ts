import { describe, expect, it, vi } from "vitest";
import { Decimal } from "decimal.js";
import type { BillingStore } from "../src/billing/billing-store.js";
import {
  boundedDiagnosticMessage,
  optionalBoundedDiagnosticMessage,
} from "../src/shared/diagnostics.js";
import { BillingEventHandlers } from "../src/billing/event-handlers.js";
import { BillingEventProcessor } from "../src/billing/event-processor.js";
import { BillingEventRepository } from "../src/billing/postgres/repositories/event.js";
import { BillingSubscriptionRepository } from "../src/billing/postgres/repositories/subscription.js";
import type {
  BillingEvent,
  BillingEventClaim,
  BillingPaymentInfo,
  BillingSubscriptionInfo,
} from "../src/billing/types/index.js";
import { BillingEventType } from "../src/billing/types/index.js";
import { StoreError } from "../src/errors.js";
import type { JsonObject } from "../src/shared/json.js";

const CLAIM_TOKEN = "00000000-0000-0000-0000-000000000003";
const BILLING_EVENT_ID = "00000000-0000-0000-0000-000000000004";

function testStore(value: Partial<BillingStore>): BillingStore {
  // SAFETY: Each fixture implements the BillingStore methods exercised by its scenario.
  return value as BillingStore;
}

interface InvalidEventFields {
  unexpected?: boolean;
  payment?: JsonObject;
  subscription?: JsonObject;
}
interface InvalidBillingEventCandidate extends BillingEvent {
  payment: BillingPaymentInfo & JsonObject;
  subscription: BillingSubscriptionInfo & JsonObject;
  unexpected?: boolean;
}
type InvalidEventFixture = [description: string, invalid: InvalidEventFields];

function claimedStore() {
  const claimBillingEvent = vi.fn<BillingStore["claimBillingEvent"]>().mockResolvedValue({
    status: "claimed",
    claimToken: CLAIM_TOKEN,
    billingEventId: BILLING_EVENT_ID,
  });
  return {
    claimBillingEvent,
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
  it.each<InvalidEventFixture>([
    ["unknown top-level fields", { unexpected: true }],
    ["invalid nested statuses", { payment: { status: "mystery" } }],
    ["coerced nested booleans", { subscription: { cancelAtPeriodEnd: "false" } }],
  ])("rejects %s before claiming the event", async (_description, invalid) => {
    const store = claimedStore();
    const processor = new BillingEventProcessor(testStore(store));
    const payment: BillingPaymentInfo & JsonObject = {
      providerPaymentId: "pay_1",
      amountMinor: 100,
      taxMinor: 0,
      currency: "USD",
      purpose: "credit_topup",
      status: "succeeded",
    };
    if (invalid.payment) Object.assign(payment, invalid.payment);
    const subscription: BillingSubscriptionInfo & JsonObject = {
      providerSubscriptionId: "sub_1",
    };
    if (invalid.subscription) Object.assign(subscription, invalid.subscription);
    const candidate: InvalidBillingEventCandidate = {
      provider: "stripe",
      eventId: "evt_invalid",
      eventType: BillingEventType.PAYMENT_SUCCEEDED,
      occurredAt: "2026-08-05T00:00:00Z",
      payment,
      subscription,
    };
    if (invalid.unexpected === true) candidate.unexpected = true;

    await expect(processor.ingestBillingEvent(candidate)).rejects.toThrow(TypeError);
    expect(store.claimBillingEvent).not.toHaveBeenCalled();
  });

  it.each([
    [{ status: "invalid_request" }, "invalid_request"],
    [{ status: "idempotency_conflict", billingEventId: BILLING_EVENT_ID }, "idempotency_conflict"],
    [{ status: "max_retries_exceeded", billingEventId: BILLING_EVENT_ID }, "max_retries_exceeded"],
    [{ status: "retry" }, "claim_failed_retry"],
  ] satisfies Array<[BillingEventClaim, string]>)(
    "surfaces an unclaimed event as %s",
    async (claim, expectedError) => {
      const store = claimedStore();
      store.claimBillingEvent.mockResolvedValue(claim);
      const processor = new BillingEventProcessor(testStore(store));

      const result = await processor.ingestBillingEvent({
        ...event(`evt_${expectedError}`, BillingEventType.INVOICE_CREATED),
        invoice: {
          providerInvoiceId: `in_${expectedError}`,
          status: "draft",
          amountPaidMinor: 0,
          amountDueMinor: 1000,
          currency: "USD",
        },
      });

      expect(result).toEqual({ handled: false, error: expectedError });
      expect(store.completeBillingEvent).not.toHaveBeenCalled();
      expect(store.failBillingEvent).not.toHaveBeenCalled();
    },
  );

  it("reports and requeues a rejected completion", async () => {
    const store = claimedStore();
    store.completeBillingEvent.mockResolvedValue(false);
    const processor = new BillingEventProcessor(testStore(store));

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
    const processor = new BillingEventProcessor(testStore(store));

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

  it.each(["   ", `  ${"x".repeat(9_000)}  `])(
    "persists only the processing error type for %j",
    async (rawMessage) => {
      const store = claimedStore();
      store.upsertBillingCustomer.mockRejectedValue(new Error(rawMessage));
      const processor = new BillingEventProcessor(testStore(store));

      const result = await processor.ingestBillingEvent({
        ...event("evt_failure_message", BillingEventType.CUSTOMER_CREATED),
        accountId: "00000000-0000-0000-0000-000000000001",
        customer: { providerCustomerId: "cus_failure" },
      });

      expect(result).toEqual({
        handled: false,
        error: "billing_event_processing_failed:Error",
      });
      expect(store.failBillingEvent).toHaveBeenCalledWith(
        "stripe",
        "evt_failure_message",
        CLAIM_TOKEN,
        "billing_event_processing_failed:Error",
      );
    },
  );
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

  it.each(["idempotency_conflict", "max_retries_exceeded"] as const)(
    "rejects a malformed %s claim without its stored event id",
    async (status) => {
      const repository = new BillingEventRepository(
        vi.fn().mockResolvedValue([{ result: status, event_id: null, claim_token: null }]),
      );

      await expect(
        repository.claim("stripe", "evt_malformed", "invoice.paid", "{}"),
      ).rejects.toMatchObject({ name: StoreError.name, indeterminate: true });
    },
  );

  it("calls and validates the canonical nine-argument entitlement RPC", async () => {
    const query = vi.fn().mockResolvedValue([{ outcome: "applied" }]);
    const repository = new BillingSubscriptionRepository(query);

    await expect(
      repository.reconcileEntitlement(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        BILLING_EVENT_ID,
        "active",
        "2026-08-05T00:00:00Z",
        null,
        true,
        null,
        "subscription_active",
      ),
    ).resolves.toBe("applied");
    expect(query).toHaveBeenCalledOnce();
    expect(query.mock.calls[0]?.[0]).toContain("reconcile_subscription_entitlement");
    expect(query.mock.calls[0]?.[1]).toHaveLength(9);

    query.mockResolvedValueOnce([{ outcome: "unexpected" }]);
    await expect(
      repository.reconcileEntitlement(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        BILLING_EVENT_ID,
        "active",
        "2026-08-05T00:00:00Z",
        null,
        true,
        null,
        "subscription_active",
      ),
    ).rejects.toMatchObject({ name: StoreError.name, indeterminate: true });
  });
});

describe("subscription plan-change provisioning", () => {
  it("reconciles terminal creation through the atomic entitlement boundary", async () => {
    const reconcileSubscriptionEntitlement = vi.fn().mockResolvedValue("revoked");
    const upsertBillingSubscription = vi.fn().mockResolvedValue(undefined);
    const persisted = {
      userId: "user-1",
      provider: "stripe",
      providerSubscriptionId: "sub-terminal",
      subscriptionId: "00000000-0000-0000-0000-000000000001",
      offerId: "00000000-0000-0000-0000-000000000010",
      offerKey: "pro_monthly",
      plan: "pro",
      status: "canceled" as const,
      providerUpdatedAt: "2026-08-05T00:00:00.000Z",
      cancelAtPeriodEnd: true,
    };
    const handlers = new BillingEventHandlers(
      testStore({
        getBillingSubscription: vi.fn().mockResolvedValue(persisted),
        getUserSubscriptions: vi.fn().mockResolvedValue([]),
        upsertBillingSubscription,
        reconcileSubscriptionEntitlement,
      }),
      {
        terminalPlanKey: "free",
        provisioning: {
          getUserPlan: vi.fn(),
          setUserPlan: vi.fn(),
          unsetUserPlan: vi.fn(),
        },
      },
    );

    const handler = handlers.getHandler(BillingEventType.SUBSCRIPTION_CREATED);
    await expect(
      handler?.({
        provider: "stripe",
        eventId: "evt_terminal_created",
        eventType: BillingEventType.SUBSCRIPTION_CREATED,
        occurredAt: "2026-08-05T00:00:00Z",
        accountId: "user-1",
        billingEventId: BILLING_EVENT_ID,
        subscription: {
          providerSubscriptionId: "sub-terminal",
          status: "canceled",
        },
      }),
    ).resolves.toMatchObject({ handled: true, action: "subscription_created" });
    expect(upsertBillingSubscription).toHaveBeenCalledOnce();
    expect(reconcileSubscriptionEntitlement).toHaveBeenCalledWith(
      "user-1",
      "00000000-0000-0000-0000-000000000001",
      BILLING_EVENT_ID,
      "canceled",
      "2026-08-05T00:00:00Z",
      null,
      true,
      "free",
      "subscription_canceled",
    );
  });

  it("suppresses cycle grants and callbacks for a stale renewal", async () => {
    const store = claimedStore();
    const callback = vi.fn().mockResolvedValue(undefined);
    const grant = vi.fn().mockResolvedValue("grant-1");
    Object.assign(store, {
      getBillingSubscription: vi.fn().mockResolvedValue({
        userId: "user-1",
        provider: "stripe",
        providerSubscriptionId: "sub-stale",
        subscriptionId: "00000000-0000-0000-0000-000000000002",
        offerId: "00000000-0000-0000-0000-000000000010",
        offerKey: "cycle_monthly",
        plan: "pro",
        status: "active",
        providerUpdatedAt: "2026-08-06T00:00:00.000Z",
        cancelAtPeriodEnd: false,
      }),
      resolveBillingOffer: vi.fn().mockResolvedValue({
        offerId: "00000000-0000-0000-0000-000000000010",
        offerKey: "cycle_monthly",
        plan: "pro",
        interval: "month",
        intervalCount: 1,
        grant: { mode: "cycle_grant", credits: new Decimal("10") },
      }),
      upsertBillingSubscription: vi.fn().mockResolvedValue(undefined),
      reconcileSubscriptionEntitlement: vi.fn().mockResolvedValue("stale"),
      createBillingCreditGrant: grant,
    });
    const processor = new BillingEventProcessor(testStore(store), {
      provisioning: {
        getUserPlan: vi.fn(),
        setUserPlan: vi.fn(),
        unsetUserPlan: vi.fn(),
      },
      eventHandlers: { [BillingEventType.SUBSCRIPTION_RENEWED]: callback },
    });

    await expect(
      processor.ingestBillingEvent({
        provider: "stripe",
        eventId: "evt-stale",
        eventType: BillingEventType.SUBSCRIPTION_RENEWED,
        occurredAt: "2026-08-05T00:00:00Z",
        accountId: "user-1",
        subscription: {
          providerSubscriptionId: "sub-stale",
          status: "active",
          refs: { priceId: "price-cycle" },
        },
      }),
    ).resolves.toEqual({ handled: true, action: "stale_subscription_event" });
    expect(grant).not.toHaveBeenCalled();
    expect(callback).not.toHaveBeenCalled();
    expect(store.completeBillingEvent).toHaveBeenCalledOnce();
  });

  it("suppresses composite invoice and checkout effects for a stale subscription payment", async () => {
    const store = claimedStore();
    const updateCheckoutIntent = vi.fn().mockResolvedValue(undefined);
    const upsertBillingInvoice = vi.fn().mockResolvedValue(undefined);
    const callback = vi.fn().mockResolvedValue(undefined);
    Object.assign(store, {
      getBillingSubscription: vi.fn().mockResolvedValue({
        userId: "user-1",
        provider: "stripe",
        providerSubscriptionId: "sub-stale-payment",
        subscriptionId: "00000000-0000-0000-0000-000000000002",
        offerId: "00000000-0000-0000-0000-000000000010",
        offerKey: "pro_monthly",
        plan: "pro",
        status: "active",
        providerUpdatedAt: "2026-08-06T00:00:00.000Z",
        cancelAtPeriodEnd: false,
      }),
      resolveBillingOffer: vi.fn().mockResolvedValue({
        offerId: "00000000-0000-0000-0000-000000000010",
        offerKey: "pro_monthly",
        plan: "pro",
        interval: "month",
        intervalCount: 1,
      }),
      upsertBillingPayment: vi.fn().mockResolvedValue("payment-1"),
      upsertBillingSubscription: vi.fn().mockResolvedValue(undefined),
      reconcileSubscriptionEntitlement: vi.fn().mockResolvedValue("stale"),
      updateCheckoutIntent,
      upsertBillingInvoice,
    });
    const processor = new BillingEventProcessor(testStore(store), {
      provisioning: {
        getUserPlan: vi.fn(),
        setUserPlan: vi.fn(),
        unsetUserPlan: vi.fn(),
      },
      eventHandlers: { [BillingEventType.PAYMENT_SUCCEEDED]: callback },
    });

    await expect(
      processor.ingestBillingEvent({
        provider: "stripe",
        eventId: "evt-stale-payment",
        eventType: BillingEventType.PAYMENT_SUCCEEDED,
        occurredAt: "2026-08-05T00:00:00Z",
        accountId: "user-1",
        metadata: { checkout_intent_id: "00000000-0000-0000-0000-000000000020" },
        subscription: {
          providerSubscriptionId: "sub-stale-payment",
          status: "active",
          refs: { priceId: "price-pro" },
        },
        payment: {
          providerPaymentId: "pay-stale",
          amountMinor: 1000,
          taxMinor: 0,
          currency: "USD",
          purpose: "subscription",
          status: "succeeded",
        },
      }),
    ).resolves.toEqual({ handled: true, action: "stale_subscription_event" });
    expect(updateCheckoutIntent).not.toHaveBeenCalled();
    expect(upsertBillingInvoice).not.toHaveBeenCalled();
    expect(callback).not.toHaveBeenCalled();
  });

  it("captures the allowance anchor before advancing the durable change", async () => {
    const anchor = new Date("2026-07-01T00:00:00.000Z");
    const updateBillingSubscriptionChange = vi.fn().mockResolvedValue(undefined);
    const upsertBillingSubscription = vi.fn().mockResolvedValue(undefined);
    const reconcileSubscriptionEntitlement = vi.fn().mockResolvedValue("applied");
    const getUserPlan = vi.fn().mockResolvedValue({ planAssignedAt: anchor });
    const store = {
      getBillingSubscription: vi.fn().mockResolvedValue({
        userId: "user-1",
        provider: "dodo",
        providerSubscriptionId: "sub-1",
        subscriptionId: "00000000-0000-0000-0000-000000000011",
        offerId: "00000000-0000-0000-0000-000000000012",
        offerKey: "monk_monthly",
        plan: "monk",
        status: "active",
        providerUpdatedAt: "2026-08-04T00:00:00.000Z",
        cancelAtPeriodEnd: false,
      }),
      resolveBillingOffer: vi.fn().mockResolvedValue({
        offerId: "00000000-0000-0000-0000-000000000013",
        offerKey: "sage_monthly",
        plan: "sage",
      }),
      getOpenBillingSubscriptionChange: vi.fn().mockResolvedValue({ id: "change-1" }),
      updateBillingSubscriptionChange,
      upsertBillingSubscription,
      reconcileSubscriptionEntitlement,
    };

    const handlers = new BillingEventHandlers(testStore(store), {
      autoSelectEntitlementSource: false,
      provisioning: {
        getUserPlan,
        setUserPlan: vi.fn(),
        unsetUserPlan: vi.fn(),
      },
    });

    const handler = handlers.getHandler(BillingEventType.SUBSCRIPTION_PLAN_CHANGED);
    await handler?.({
      provider: "dodo",
      eventId: "evt_plan_change_with_pending",
      eventType: BillingEventType.SUBSCRIPTION_PLAN_CHANGED,
      occurredAt: "2026-08-05T00:00:00Z",
      accountId: "user-1",
      billingEventId: BILLING_EVENT_ID,
      subscription: {
        providerSubscriptionId: "sub-1",
        status: "active",
        refs: { productId: "product-sage" },
      },
    });

    expect(getUserPlan).toHaveBeenCalledTimes(1);
    expect(getUserPlan.mock.invocationCallOrder[0]).toBeLessThan(
      upsertBillingSubscription.mock.invocationCallOrder[0]!,
    );
    expect(upsertBillingSubscription.mock.invocationCallOrder[0]).toBeLessThan(
      reconcileSubscriptionEntitlement.mock.invocationCallOrder[0]!,
    );
    expect(reconcileSubscriptionEntitlement.mock.invocationCallOrder[0]).toBeLessThan(
      updateBillingSubscriptionChange.mock.invocationCallOrder[0]!,
    );
    expect(reconcileSubscriptionEntitlement).toHaveBeenCalledWith(
      "user-1",
      "00000000-0000-0000-0000-000000000011",
      BILLING_EVENT_ID,
      "active",
      "2026-08-05T00:00:00Z",
      anchor,
      false,
      null,
      "subscription_active",
    );
  });

  it("still versions plan changes when automatic entitlement is disabled", async () => {
    const reconcileSubscriptionEntitlement = vi.fn().mockResolvedValue("preserved");
    const store = {
      getBillingSubscription: vi.fn().mockResolvedValue({
        userId: "user-1",
        provider: "dodo",
        providerSubscriptionId: "sub-1",
        subscriptionId: "00000000-0000-0000-0000-000000000011",
        offerId: "00000000-0000-0000-0000-000000000012",
        offerKey: "monk_monthly",
        plan: "monk",
        status: "active",
        providerUpdatedAt: "2026-08-04T00:00:00.000Z",
        cancelAtPeriodEnd: false,
      }),
      resolveBillingOffer: vi.fn().mockResolvedValue({
        offerId: "00000000-0000-0000-0000-000000000013",
        offerKey: "sage_monthly",
        plan: "sage",
      }),
      getOpenBillingSubscriptionChange: vi.fn().mockResolvedValue(null),
      upsertBillingSubscription: vi.fn().mockResolvedValue(undefined),
      reconcileSubscriptionEntitlement,
    };

    const handlers = new BillingEventHandlers(testStore(store), {
      autoSelectEntitlementSource: false,
      provisioning: {
        getUserPlan: vi.fn().mockResolvedValue(null),
        setUserPlan: vi.fn(),
        unsetUserPlan: vi.fn(),
      },
    });

    const handler = handlers.getHandler(BillingEventType.SUBSCRIPTION_PLAN_CHANGED);
    await handler?.({
      provider: "dodo",
      eventId: "evt_plan_change",
      eventType: BillingEventType.SUBSCRIPTION_PLAN_CHANGED,
      occurredAt: "2026-08-05T00:00:00Z",
      accountId: "user-1",
      billingEventId: BILLING_EVENT_ID,
      subscription: {
        providerSubscriptionId: "sub-1",
        status: "active",
        periodStart: "2030-01-01T00:00:00.000Z",
        refs: { productId: "product-sage" },
      },
    });

    expect(reconcileSubscriptionEntitlement).toHaveBeenCalledOnce();
    expect(reconcileSubscriptionEntitlement.mock.calls[0]?.[6]).toBe(false);
    expect(reconcileSubscriptionEntitlement.mock.calls[0]?.[5]).toBeNull();
  });
});
