import { Decimal } from "decimal.js";
import { expect, it, vi } from "vitest";

import type { AutoRechargeBillingPort } from "../src/billing/auto-recharge-port.js";
import { AutoRechargeService } from "../src/billing/auto-recharge-service.js";
import type { PaymentProvider } from "../src/providers/types.js";

const catalog = {
  version: 1,
  credits: {
    buckets: { purchased: { priority: 10 } },
    default_bucket: "purchased",
  },
  commerce: {
    providers: { stripe: { type: "stripe" } },
    offers: {
      small_pack: {
        type: "topup",
        display_name: "1,000 credits",
        price: { amount_minor: 500, currency: "USD" },
        providers: { stripe: { type: "stripe_price", price_id: "price_pack" } },
        credits_per_unit: "1000",
        quantity: { minimum: 1, maximum: 3, default: 1 },
        bucket: "purchased",
      },
    },
    auto_recharge: {
      eligible_topups: ["small_pack"],
      balance_below: { minimum: "100", maximum: "5000", default: "1000" },
      rearm_above: "6000",
      quantity: { minimum: 1, maximum: 3, default: 1 },
      limits: {
        max_purchases: 3,
        window: { type: "rolling", duration: { unit: "day", count: 30 } },
        max_charge_minor: 1500,
        cooldown: { unit: "hour", count: 1 },
      },
    },
  },
};

it("resubmits an unknown auto-recharge outcome with its original provider key", async () => {
  const chargeSavedPaymentMethod = vi.fn().mockResolvedValue({
    providerPaymentId: "payment-1",
    status: "succeeded" as const,
  });
  const provider: PaymentProvider = {
    provider: "stripe",
    createCheckoutSession: vi.fn(),
    handleWebhook: vi.fn(),
    listPaymentMethods: vi.fn().mockResolvedValue([
      {
        id: "method-1",
        last4: "4242",
        brand: "visa",
        expiryMonth: 12,
        expiryYear: 2030,
        isDefault: true,
      },
    ]),
    chargeSavedPaymentMethod,
  };
  const updateAutoRechargeAttempt = vi.fn().mockResolvedValue(undefined);
  const billing: AutoRechargeBillingPort = {
    getActiveCatalogDocument: vi.fn().mockResolvedValue(catalog),
    resolveTopup: vi.fn().mockResolvedValue({
      topupId: "00000000-0000-0000-0000-000000000010",
      topupKey: "small_pack",
      creditsPerUnit: new Decimal(1000),
      depositTo: "purchased",
      amountMinor: 500,
      currency: "USD",
      minQuantity: 1,
      maxQuantity: 3,
      defaultQuantity: 1,
      minAmountMinor: 500,
      maxAmountMinor: 1500,
    }),
    resolveTopupByLookup: vi.fn(),
    getCustomerByUserId: vi.fn().mockResolvedValue({
      provider: "stripe",
      providerCustomerId: "customer-1",
    }),
    getAutoRechargeProfile: vi.fn().mockResolvedValue({
      userId: "00000000-0000-0000-0000-000000000001",
      enabled: true,
      state: "active",
      provider: "stripe",
      topupId: "00000000-0000-0000-0000-000000000010",
      topupKey: "small_pack",
      quantity: 1,
      threshold: new Decimal(1000),
      maxChargesPerWindow: 3,
      windowUnit: "day",
      windowCount: 30,
      windowAnchor: "rolling",
      windowTimezone: "UTC",
    }),
    upsertAutoRechargeProfile: vi.fn(),
    claimAutoRechargeAttempt: vi.fn().mockResolvedValue({
      id: "00000000-0000-0000-0000-000000000020",
      userId: "00000000-0000-0000-0000-000000000001",
      provider: "stripe",
      idempotencyKey: "auto-recharge:original",
      providerAttemptId: null,
      topupId: "00000000-0000-0000-0000-000000000010",
      quantity: 1,
      state: "unknown",
      windowStart: "2026-08-01T00:00:00.000Z",
      windowEnd: "2026-08-31T00:00:00.000Z",
      quotedAmountMinor: null,
      currency: null,
      failureCode: "provider_request_failed",
      failureMessage: "connection reset",
      metadata: {},
      createdAt: "2026-08-01T00:00:00.000Z",
      updatedAt: "2026-08-01T00:00:01.000Z",
    }),
    updateAutoRechargeAttempt,
    getAutoRechargeOperationalWindow: vi.fn().mockResolvedValue({
      maxRecharges: 3,
      windowStart: "2026-01-01T00:00:00.000Z",
      windowEnd: "2026-01-31T00:00:00.000Z",
      count: 1,
    }),
    resumeAutoRechargeProfile: vi.fn().mockResolvedValue(true),
    countAutoRechargeAttempts: vi.fn().mockResolvedValue(1),
  };

  await expect(
    new AutoRechargeService(billing).processIfNeeded({
      userId: "00000000-0000-0000-0000-000000000001",
      provider,
      balance: new Decimal(50),
    }),
  ).resolves.toMatchObject({ outcome: "submitted" });
  expect(chargeSavedPaymentMethod).toHaveBeenCalledWith(
    expect.objectContaining({
      idempotencyKey: "auto-recharge:original",
      metadata: {
        auto_recharge_attempt_id: "00000000-0000-0000-0000-000000000020",
        bursar_account_id: "00000000-0000-0000-0000-000000000001",
        purpose: "credit_topup",
      },
    }),
  );
  expect(updateAutoRechargeAttempt).toHaveBeenCalledWith(
    expect.objectContaining({
      state: "processing",
      providerAttemptId: "payment-1",
    }),
  );
  const reordered = structuredClone(catalog);
  const other = structuredClone(reordered.commerce.offers.small_pack);
  other.providers.stripe.price_id = "price_other";
  Object.assign(reordered.commerce.offers, { other_pack: other });
  reordered.commerce.auto_recharge.eligible_topups = ["other_pack", "small_pack"];
  reordered.commerce.auto_recharge.quantity.default = 2;
  vi.mocked(billing.getActiveCatalogDocument).mockResolvedValue(reordered);
  chargeSavedPaymentMethod.mockClear();
  vi.mocked(billing.claimAutoRechargeAttempt).mockClear();
  await new AutoRechargeService(billing).processIfNeeded({
    userId: "00000000-0000-0000-0000-000000000001",
    provider,
    balance: new Decimal(50),
  });
  expect(billing.claimAutoRechargeAttempt).toHaveBeenCalledWith(
    expect.objectContaining({
      expectedTopupKey: "small_pack",
      expectedProductId: "price_pack",
      expectedQuantity: 1,
    }),
  );
  expect(chargeSavedPaymentMethod).toHaveBeenCalledWith(
    expect.objectContaining({ productId: "price_pack", quantity: 1 }),
  );

  for (const guardrail of ["charge", "threshold"] as const) {
    const changed = structuredClone(catalog);
    if (guardrail === "charge") changed.commerce.auto_recharge.limits.max_charge_minor = 100;
    else {
      changed.commerce.auto_recharge.balance_below.maximum = "500";
      changed.commerce.auto_recharge.balance_below.default = "500";
    }
    vi.mocked(billing.getActiveCatalogDocument).mockResolvedValue(changed);
    vi.mocked(billing.claimAutoRechargeAttempt).mockClear();
    chargeSavedPaymentMethod.mockClear();
    await expect(
      new AutoRechargeService(billing).processIfNeeded({
        userId: "00000000-0000-0000-0000-000000000001",
        provider,
        balance: new Decimal(50),
      }),
    ).resolves.toMatchObject({ outcome: "not_configured" });
    expect(billing.claimAutoRechargeAttempt).not.toHaveBeenCalled();
    expect(chargeSavedPaymentMethod).not.toHaveBeenCalled();
  }

  // A previously enabled profile cannot trigger a charge after its offer
  // becomes unavailable; no trusted region exists for unattended charges.
  for (const availability of [
    { regions: ["IN"] },
    { starts_at: "2099-01-01T00:00:00Z" },
    { ends_at: "2000-01-01T00:00:00Z" },
  ]) {
    const restricted = structuredClone(catalog);
    Object.assign(restricted.commerce.offers.small_pack, { availability });
    vi.mocked(billing.getActiveCatalogDocument).mockResolvedValue(restricted);
    vi.mocked(billing.claimAutoRechargeAttempt).mockClear();
    chargeSavedPaymentMethod.mockClear();
    await expect(
      new AutoRechargeService(billing).processIfNeeded({
        userId: "00000000-0000-0000-0000-000000000001",
        provider,
        balance: new Decimal(50),
      }),
    ).resolves.toMatchObject({ outcome: "not_configured" });
    expect(billing.claimAutoRechargeAttempt).not.toHaveBeenCalled();
    expect(chargeSavedPaymentMethod).not.toHaveBeenCalled();
  }
});
