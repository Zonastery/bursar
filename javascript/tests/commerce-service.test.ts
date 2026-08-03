import Decimal from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import type { BillingCapability, BillingEventSink } from "../src/billing/contracts.js";
import type { CheckoutIntent } from "../src/billing/types/index.js";
import {
  CheckoutConflictError,
  CheckoutCompletedError,
  CoreBillingDataUnavailableError,
  InvalidOfferQuantityError,
  ProviderCapabilityNotSupportedError,
  QuoteChangedError,
  UnknownOfferError,
} from "../src/commerce/errors.js";
import { CommerceService } from "../src/commerce/service.js";
import type { CommerceOptions } from "../src/commerce/types.js";
import type { CreditsService } from "../src/credits/service.js";
import type { PaymentProvider } from "../src/providers/types.js";

function catalog() {
  return {
    version: 1,
    credits: {
      accounting: { unit: "credit", scale: 6, rounding: "half_up" },
      buckets: { general: { priority: 10 } },
      default_bucket: "general",
      policies: { prepaid: { type: "prepaid" } },
    },
    entitlements: { features: {} },
    admission: { policies: {} },
    plans: {
      basic: {
        display_name: "Basic",
        rank: 0,
        allowed_operations: [],
        features: {},
        quotas: {},
        credit_policy: "prepaid",
      },
      peer: {
        display_name: "Peer",
        rank: 0,
        allowed_operations: [],
        features: {},
        quotas: {},
        credit_policy: "prepaid",
      },
      pro: {
        display_name: "Pro",
        rank: 1,
        allowed_operations: [],
        features: {},
        quotas: {},
        credit_policy: "prepaid",
      },
    },
    commerce: {
      providers: {
        alpha: { type: "custom", adapter: "alpha" },
        beta: { type: "custom", adapter: "beta" },
      },
      offers: {
        basic_month: {
          type: "subscription",
          display_name: "Basic monthly",
          plan: "basic",
          billing_interval: { unit: "month", count: 1 },
          price: { amount_minor: 1000, currency: "USD" },
          providers: {
            alpha: {
              type: "custom_object",
              object_kind: "subscription",
              external_id: "alpha-basic-month",
            },
          },
        },
        basic_year: {
          type: "subscription",
          display_name: "Basic yearly",
          plan: "basic",
          billing_interval: { unit: "year", count: 1 },
          price: { amount_minor: 10_000, currency: "USD" },
          providers: {
            alpha: {
              type: "custom_object",
              object_kind: "subscription",
              external_id: "alpha-basic-year",
            },
          },
        },
        peer_month: {
          type: "subscription",
          display_name: "Peer monthly",
          plan: "peer",
          billing_interval: { unit: "month", count: 1 },
          price: { amount_minor: 1000, currency: "USD" },
          providers: {
            alpha: {
              type: "custom_object",
              object_kind: "subscription",
              external_id: "alpha-peer-month",
            },
          },
        },
        pro_month: {
          type: "subscription",
          display_name: "Pro monthly",
          plan: "pro",
          billing_interval: { unit: "month", count: 1 },
          price: { amount_minor: 2000, currency: "USD" },
          providers: {
            alpha: {
              type: "custom_object",
              object_kind: "subscription",
              external_id: "alpha-pro-month",
            },
          },
        },
        pack: {
          type: "topup",
          display_name: "Credit pack",
          credits_per_unit: "100",
          quantity: { minimum: 2, maximum: 5, default: 2 },
          bucket: "general",
          price: { amount_minor: 500, currency: "USD" },
          providers: {
            beta: {
              type: "custom_object",
              object_kind: "one_time",
              external_id: "beta-pack",
            },
          },
        },
      },
      subscription_changes: {
        upgrade: {
          effective: "immediate",
          proration: "prorated",
          payment_failure: "prevent_change",
        },
        downgrade: {
          effective: "renewal",
          proration: "none",
          payment_failure: "prevent_change",
        },
        lateral: {
          effective: "immediate",
          proration: "prorated",
          payment_failure: "prevent_change",
        },
        cadence_change: {
          effective: "renewal",
          proration: "none",
          payment_failure: "prevent_change",
        },
      },
    },
  };
}

function provider(name = "alpha"): PaymentProvider {
  return {
    provider: name,
    createCheckoutSession: vi.fn(async (params) => ({
      url: `https://checkout.example/${params.productId}`,
      providerSessionId: "session-1",
      customerId: "customer-1",
    })),
    handleWebhook: vi.fn(async () => ({
      received: true,
      retryable: false,
      provider: name,
      eventId: "event-1",
      eventType: "payment.succeeded",
    })),
    cancelSubscription: vi.fn(async () => undefined),
    reactivateSubscription: vi.fn(async () => undefined),
    previewChangePlan: vi.fn(async () => ({
      totalAmount: 100,
      settlementAmount: 100,
      currency: "USD",
      lineItems: [],
      effectiveAt: "2026-08-01T00:00:00.000Z",
      nextBillingDate: "2026-09-01T00:00:00.000Z",
    })),
    changePlan: vi.fn(async () => ({ providerOperationId: "change-1" })),
    cancelScheduledPlanChange: vi.fn(async () => undefined),
    createCustomerPortalSession: vi.fn(async () => ({
      url: "https://portal.example/session",
    })),
    listPaymentMethods: vi.fn(async () => []),
    getInvoiceUrl: vi.fn(async () => ({ url: "https://invoice.example/document" })),
  };
}

function intent(overrides: Partial<CheckoutIntent> = {}): CheckoutIntent {
  return {
    id: "intent-1",
    subjectId: "subject-1",
    provider: "beta",
    checkoutKind: "credit_topup",
    productKey: "pack",
    requestDigest: "",
    status: "open",
    expiresAt: "2099-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function harness(input?: {
  alpha?: PaymentProvider;
  beta?: PaymentProvider;
  options?: Partial<CommerceOptions>;
}) {
  const alpha = input?.alpha ?? provider("alpha");
  const beta = input?.beta ?? provider("beta");
  const billing = {
    autoRecharge: {
      getStatus: vi.fn(async () => null),
      enable: vi.fn(async () => null),
      disable: vi.fn(async () => undefined),
      retry: vi.fn(async () => ({ outcome: "above_threshold" })),
      processIfNeeded: vi.fn(async () => ({ outcome: "above_threshold" })),
    },
    getActiveBursarConfig: vi.fn(async () => catalog()),
    getUserSubscription: vi.fn(async () => null),
    getActiveSubscription: vi.fn(async () => null),
    getBlockingSubscription: vi.fn(async () => null),
    getCustomerByUserId: vi.fn(async () => null),
    createOrGetCheckoutIntent: vi.fn(async (value) =>
      intent({ requestDigest: value.requestDigest, provider: value.provider }),
    ),
    updateCheckoutIntent: vi.fn(async () => undefined),
    getCheckoutIntent: vi.fn(async () => null),
    upsertCustomer: vi.fn(async () => undefined),
    ingestBillingEvent: vi.fn(async () => ({ processed: true })),
    getUserPreferences: vi.fn(async () => null),
    updateUserPreferences: vi.fn(async () => undefined),
    listBillingInvoices: vi.fn(async () => []),
    getOpenBillingSubscriptionChange: vi.fn(async () => null),
    createBillingSubscriptionChange: vi.fn(async (value) => ({
      id: "change-row",
      subscriptionId: "subscription-row",
      fromOfferId: "offer-basic",
      toOfferId: value.toOfferId,
      fromOffer: { offerId: "offer-basic", offerKey: "basic_month" },
      toOffer: { offerId: value.toOfferId, offerKey: "pro_month" },
      effectiveAt: value.effectiveAt,
      effective: value.effective,
      state: value.effective === "renewal" ? "scheduled" : "awaiting_payment",
      prorationBehavior: value.prorationBehavior ?? "provider_default",
      idempotencyKey: value.idempotencyKey,
    })),
    updateBillingSubscriptionChange: vi.fn(async () => undefined),
    resolveOffer: vi.fn(async () => null),
    resolveOfferByLookup: vi.fn(async (_providerName, lookupKey) => ({
      offerId: `persisted-${lookupKey}`,
      offerKey: lookupKey,
    })),
    getAutoRechargeProfile: vi.fn(async () => null),
  };
  const credits = {
    getBalance: vi.fn(async () => ({
      balance: new Decimal(25),
      lifetimePurchased: new Decimal(100),
    })),
    getAvailable: vi.fn(async () => ({ available: new Decimal(30) })),
    getBucketBalances: vi.fn(async () => ({
      buckets: [{ bucketKey: "general", balance: new Decimal(30) }],
    })),
    getUserPlan: vi.fn(async () => ({
      planKey: "basic",
      entitlements: {},
      allowedOperations: [],
      allowance: {
        amount: new Decimal(10),
        resetUnit: "month",
        resetCount: 1,
        resetAnchor: "calendar",
        resetTimezone: "UTC",
      },
    })),
    checkAllowance: vi.fn(async () => ({
      allowed: true,
      allowanceRemaining: new Decimal(5),
      periodStart: null,
      periodEnd: null,
    })),
    listLedgerEntries: vi.fn(async () => ({ items: [], hasMore: false })),
    listUsageEntries: vi.fn(async () => ({ items: [], hasMore: false })),
    listUsageCharges: vi.fn(async () => ({ items: [], nextCursor: null })),
    getLedgerEntry: vi.fn(async () => null),
  };
  const sink = { ingestBillingEvent: vi.fn(async () => ({ processed: true })) };
  const alphaFactory = vi.fn(async () => alpha);
  const betaFactory = vi.fn(async () => beta);
  const service = new CommerceService(
    billing as unknown as BillingCapability,
    credits as unknown as Pick<CreditsService, keyof CreditsService>,
    sink as unknown as BillingEventSink,
    {
      providers: { alpha: alphaFactory, beta: betaFactory },
      ...input?.options,
    },
  );
  return {
    service,
    billing,
    credits,
    alpha,
    beta,
    alphaFactory,
    betaFactory,
  };
}

function activeSubscription(overrides = {}) {
  return {
    userId: "user-1",
    provider: "alpha",
    providerSubscriptionId: "subscription-1",
    providerCustomerId: "customer-1",
    plan: "basic",
    interval: "month",
    status: "active" as const,
    cancelAtPeriodEnd: false,
    ...overrides,
  };
}

describe("CommerceService", () => {
  it("validates the active subscription plan when entitlements lag billing state", async () => {
    const { service, billing, credits } = harness();
    billing.getUserSubscription.mockResolvedValue(activeSubscription());
    const currentPlan = await credits.getUserPlan("user-1");
    credits.getUserPlan.mockResolvedValue({ ...currentPlan, planKey: null });

    await expect(service.getAccountSubscriptionSummary("user-1")).resolves.toMatchObject({
      planKey: "basic",
      isCurrent: true,
      isEntitled: false,
    });

    billing.getUserSubscription.mockResolvedValue(activeSubscription({ plan: "missing" }));
    await expect(service.getAccountSubscriptionSummary("user-1")).rejects.toBeInstanceOf(
      CoreBillingDataUnavailableError,
    );
  });

  it("lazily selects the provider referenced by an offer without an implicit default", async () => {
    const { service, betaFactory, alphaFactory, beta } = harness();

    const result = await service.createCheckout({
      subjectId: "subject-1",
      offerKey: "pack",
      type: "credit_pack",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey: "operation-1",
    });

    expect(result).toMatchObject({ provider: "beta", offerKey: "pack", intentId: "intent-1" });
    expect(betaFactory).toHaveBeenCalledOnce();
    expect(alphaFactory).not.toHaveBeenCalled();
    expect(beta.createCheckoutSession).toHaveBeenCalledWith(
      expect.objectContaining({
        quantity: 2,
        productId: "beta-pack",
        returnUrl: "https://app.example/return?intent=intent-1",
      }),
    );
    await service.getProvider("beta");
    expect(betaFactory).toHaveBeenCalledOnce();
  });

  it("resolves only catalog offer keys while enforcing offer type and quantity", async () => {
    const { service } = harness();
    await expect(
      service.createCheckout({
        subjectId: "subject-1",
        offerKey: "missing",
        returnUrl: "https://app.example",
        cancelUrl: "https://app.example",
        operationKey: "operation-1",
      }),
    ).rejects.toBeInstanceOf(UnknownOfferError);
    await expect(
      service.createCheckout({
        subjectId: "subject-1",
        offerKey: "pack",
        type: "subscription",
        returnUrl: "https://app.example",
        cancelUrl: "https://app.example",
        operationKey: "operation-2",
      }),
    ).rejects.toBeInstanceOf(UnknownOfferError);
    await expect(
      service.createCheckout({
        subjectId: "subject-1",
        offerKey: "pack",
        quantity: 6,
        returnUrl: "https://app.example",
        cancelUrl: "https://app.example",
        operationKey: "operation-3",
      }),
    ).rejects.toBeInstanceOf(InvalidOfferQuantityError);
  });

  it("does not replay a terminal checkout URL", async () => {
    const beta = provider("beta");
    beta.getCheckoutSessionStatus = vi.fn(async () => ({ paymentStatus: "succeeded" }));
    const { service, billing } = harness({ beta });
    billing.createOrGetCheckoutIntent.mockImplementation(async (value) =>
      intent({
        requestDigest: value.requestDigest,
        provider: value.provider,
        providerSessionId: "session-1",
        checkoutUrl: "https://checkout.example/old",
      }),
    );

    await expect(
      service.createCheckout({
        subjectId: "subject-1",
        offerKey: "pack",
        returnUrl: "https://app.example",
        cancelUrl: "https://app.example",
        operationKey: "operation-1",
      }),
    ).rejects.toBeInstanceOf(CheckoutCompletedError);
    expect(beta.createCheckoutSession).not.toHaveBeenCalled();
    expect(billing.updateCheckoutIntent).toHaveBeenCalledWith("intent-1", {
      status: "completed",
    });
  });

  it("makes subscription lifecycle no-ops without invoking providers", async () => {
    const { service, billing, alphaFactory } = harness();
    billing.getUserSubscription.mockResolvedValue(
      activeSubscription({ status: "canceled", cancelAtPeriodEnd: false }),
    );
    await expect(
      service.cancelSubscription({ accountId: "user-1", operationKey: "cancel-1" }),
    ).resolves.toEqual({ ok: true });
    billing.getUserSubscription.mockResolvedValue(activeSubscription());
    await expect(
      service.reactivateSubscription({ accountId: "user-1", operationKey: "reactivate-1" }),
    ).resolves.toEqual({ ok: true });
    expect(alphaFactory).not.toHaveBeenCalled();
  });

  it("schedules cancellation for a past-due subscription through its provider", async () => {
    const { service, billing, alpha } = harness();
    billing.getUserSubscription.mockResolvedValue(
      activeSubscription({ status: "past_due", cancelAtPeriodEnd: false }),
    );

    await expect(
      service.cancelSubscription({ accountId: "user-1", operationKey: "cancel-past-due" }),
    ).resolves.toEqual({ ok: true, pending: true });

    expect(alpha.cancelSubscription).toHaveBeenCalledWith("subscription-1", "cancel-past-due");
    expect(billing.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.cancellation_scheduled",
        userId: "user-1",
        subscription: expect.objectContaining({
          providerSubscriptionId: "subscription-1",
          cancelAtPeriodEnd: true,
        }),
      }),
    );
  });

  it.each([
    ["pro_month", "upgrade", "immediately", "prorated_immediately"],
    ["peer_month", "lateral", "immediately", "prorated_immediately"],
    ["basic_year", "cadence_change", "next_billing_date", "do_not_bill"],
  ] as const)(
    "classifies %s as %s from explicit rank and cadence",
    async (offerKey, classification, effectiveAt, prorationBillingMode) => {
      const { service, billing, alpha } = harness();
      billing.getActiveSubscription.mockResolvedValue(activeSubscription());

      const result = await service.previewPlanChange({ accountId: "user-1", offerKey });

      expect(result.classification).toBe(classification);
      expect(alpha.previewChangePlan).toHaveBeenCalledWith(
        expect.objectContaining({ effectiveAt, prorationBillingMode }),
      );
    },
  );

  it("does not misclassify an immediate no-proration change as scheduled", async () => {
    const config = catalog();
    config.commerce.subscription_changes.upgrade.proration = "none";
    const { service, billing, alpha } = harness();
    billing.getActiveBursarConfig.mockResolvedValue(config);
    billing.getActiveSubscription.mockResolvedValue(activeSubscription());

    const preview = await service.previewPlanChange({
      accountId: "user-1",
      offerKey: "pro_month",
    });
    const result = await service.confirmPlanChange({
      accountId: "user-1",
      offerKey: "pro_month",
      quoteFingerprint: preview.quoteFingerprint,
      operationKey: "immediate-without-proration",
    });

    expect(result.scheduled).toBe(false);
    expect(billing.createBillingSubscriptionChange).toHaveBeenCalledWith(
      expect.objectContaining({ effective: "immediate", prorationBehavior: "none" }),
    );
    expect(alpha.changePlan).toHaveBeenCalledWith(
      expect.objectContaining({ effectiveAt: "immediately", prorationBillingMode: "do_not_bill" }),
    );
  });

  it("refreshes a quote and rejects financially changed confirmation", async () => {
    const alpha = provider("alpha");
    vi.mocked(alpha.previewChangePlan!)
      .mockResolvedValueOnce({
        totalAmount: 100,
        settlementAmount: 100,
        currency: "USD",
        lineItems: [],
        effectiveAt: "2026-08-01T00:00:00.000Z",
      })
      .mockResolvedValueOnce({
        totalAmount: 125,
        settlementAmount: 125,
        currency: "USD",
        lineItems: [],
        effectiveAt: "2026-08-01T00:00:00.000Z",
      });
    const { service, billing } = harness({ alpha });
    billing.getActiveSubscription.mockResolvedValue(activeSubscription());
    const preview = await service.previewPlanChange({
      accountId: "user-1",
      offerKey: "pro_month",
    });

    await expect(
      service.confirmPlanChange({
        accountId: "user-1",
        offerKey: "pro_month",
        quoteFingerprint: preview.quoteFingerprint,
        operationKey: "change-1",
      }),
    ).rejects.toBeInstanceOf(QuoteChangedError);
    expect(billing.createBillingSubscriptionChange).not.toHaveBeenCalled();
    expect(alpha.changePlan).not.toHaveBeenCalled();
  });

  it("requires explicit cancellation before replacing a scheduled plan change", async () => {
    const alpha = provider("alpha");
    const { service, billing } = harness({ alpha });
    billing.getActiveSubscription.mockResolvedValue(activeSubscription());
    const existing = {
      id: "old-change",
      subscriptionId: "subscription-row",
      fromOfferId: "offer-basic",
      toOfferId: "offer-pro",
      fromOffer: { offerId: "offer-basic", offerKey: "basic_month" },
      toOffer: { offerId: "offer-pro", offerKey: "pro_month" },
      effectiveAt: "2026-09-01T00:00:00.000Z",
      effective: "renewal" as const,
      state: "scheduled" as const,
      prorationBehavior: "none" as const,
      idempotencyKey: "old-operation",
    };
    billing.getOpenBillingSubscriptionChange.mockResolvedValue(existing);
    const preview = await service.previewPlanChange({
      accountId: "user-1",
      offerKey: "pro_month",
    });

    await expect(
      service.confirmPlanChange({
        accountId: "user-1",
        offerKey: "pro_month",
        quoteFingerprint: preview.quoteFingerprint,
        operationKey: "new-operation",
      }),
    ).rejects.toThrow(CheckoutConflictError);

    expect(alpha.cancelScheduledPlanChange).not.toHaveBeenCalled();
    expect(billing.createBillingSubscriptionChange).not.toHaveBeenCalled();
    expect(alpha.changePlan).not.toHaveBeenCalled();
  });

  it("restores a scheduled cancellation when a provider plan change fails", async () => {
    const alpha = provider("alpha");
    vi.mocked(alpha.changePlan!).mockRejectedValue(new Error("change failed"));
    const { service, billing } = harness({ alpha });
    billing.getActiveSubscription.mockResolvedValue(
      activeSubscription({ cancelAtPeriodEnd: true }),
    );
    const preview = await service.previewPlanChange({
      accountId: "user-1",
      offerKey: "pro_month",
    });

    await expect(
      service.confirmPlanChange({
        accountId: "user-1",
        offerKey: "pro_month",
        quoteFingerprint: preview.quoteFingerprint,
        operationKey: "change-fails",
      }),
    ).rejects.toThrow("change failed");

    expect(alpha.reactivateSubscription).toHaveBeenCalledWith(
      "subscription-1",
      "change-fails:keep",
    );
    expect(alpha.cancelSubscription).toHaveBeenCalledWith(
      "subscription-1",
      "change-fails:restore-cancellation",
    );
    expect(billing.updateBillingSubscriptionChange).toHaveBeenLastCalledWith(expect.any(String), {
      state: "failed",
      errorMessage: "change failed",
    });
  });

  it("reports optional portal capabilities with a typed error", async () => {
    const alpha = provider("alpha");
    delete alpha.createCustomerPortalSession;
    const { service, billing } = harness({ alpha });
    billing.getCustomerByUserId.mockResolvedValue({
      provider: "alpha",
      providerCustomerId: "customer-1",
      userId: "user-1",
    });

    await expect(
      service.createPortalSession({
        accountId: "user-1",
        returnUrl: "https://app.example/billing",
      }),
    ).rejects.toBeInstanceOf(ProviderCapabilityNotSupportedError);
  });

  it("merges application defaults, persisted preferences, and patches", async () => {
    const { service, billing } = harness({
      options: { preferenceDefaults: { invoiceReminders: true } },
    });
    billing.getUserPreferences.mockResolvedValue({
      userId: "user-1",
      autoRecharge: true,
      overageProtection: true,
      emailNotifications: false,
      usageAlerts: true,
      invoiceReminders: true,
    });

    const result = await service.updatePreferences({
      accountId: "user-1",
      patch: { usageAlerts: false },
    });

    expect(result).toMatchObject({
      userId: "user-1",
      autoRecharge: true,
      emailNotifications: false,
      usageAlerts: false,
      invoiceReminders: true,
    });
    expect(billing.updateUserPreferences).toHaveBeenCalledWith(result);
  });

  it("returns document references and section-level availability for optional failures", async () => {
    const { service, billing, credits } = harness();
    billing.listBillingInvoices.mockResolvedValue([
      {
        provider: "alpha",
        providerInvoiceId: "invoice-1",
        status: "paid",
        amountPaidMinor: 1000,
        currency: "USD",
      },
    ]);
    credits.listLedgerEntries.mockRejectedValue(new Error("history unavailable"));
    credits.listUsageCharges.mockRejectedValue(new Error("usage unavailable"));

    const overview = await service.getAccountOverview("user-1");

    expect(overview.credits.effectiveSpendableBalance.toString()).toBe("35");
    expect(credits.listUsageCharges).toHaveBeenCalledWith("user-1", {
      limit: 100,
      includeRecordOnly: false,
    });
    expect(overview.documents).toContainEqual(
      expect.objectContaining({
        kind: "provider_invoice",
        provider: "alpha",
        providerDocumentId: "invoice-1",
      }),
    );
    expect(overview.availability).toMatchObject({
      documents: false,
      providerInvoices: true,
      transactions: false,
      usage: false,
    });
  });

  it("treats core credit-state failure as fatal", async () => {
    const { service, credits } = harness();
    credits.getBalance.mockRejectedValue(new Error("ledger unavailable"));
    await expect(service.getAccountOverview("user-1")).rejects.toBeInstanceOf(
      CoreBillingDataUnavailableError,
    );
  });

  it("verifies document ownership before resolving a provider link", async () => {
    const { service, billing, alpha } = harness();
    billing.listBillingInvoices.mockResolvedValue([
      { provider: "alpha", providerInvoiceId: "invoice-1" },
    ]);

    await expect(
      service.getInvoiceLink({
        accountId: "user-1",
        document: {
          kind: "provider_invoice",
          provider: "alpha",
          providerDocumentId: "invoice-1",
        },
      }),
    ).resolves.toEqual({ url: "https://invoice.example/document" });
    expect(alpha.getInvoiceUrl).toHaveBeenCalledWith("invoice-1");
  });

  it("uses the persisted auto-recharge provider and avoids provider work when disabled", async () => {
    const { service, billing, alphaFactory } = harness();
    await expect(service.autoRecharge.processIfNeeded({ accountId: "user-1" })).resolves.toEqual({
      outcome: "disabled",
    });
    expect(alphaFactory).not.toHaveBeenCalled();

    billing.getAutoRechargeProfile.mockResolvedValue({
      userId: "user-1",
      enabled: true,
      state: "active",
      armed: true,
      provider: "alpha",
      topupId: "topup-1",
      quantity: 1,
      threshold: 10,
      maxChargesPerWindow: 2,
      windowUnit: "month",
      windowCount: 1,
      windowAnchor: "calendar",
      windowTimezone: "UTC",
    });
    await service.autoRecharge.processIfNeeded({ accountId: "user-1" });
    expect(alphaFactory).toHaveBeenCalledOnce();
    expect(billing.autoRecharge.processIfNeeded).toHaveBeenCalledWith(
      expect.objectContaining({
        userId: "user-1",
        provider: expect.objectContaining({ provider: "alpha" }),
      }),
    );
  });
});
