import Decimal from "decimal.js";
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
    expect.objectContaining({ idempotencyKey: "auto-recharge:original" }),
  );
  expect(updateAutoRechargeAttempt).toHaveBeenCalledWith(
    expect.objectContaining({ state: "processing", providerAttemptId: "payment-1" }),
  );
});
