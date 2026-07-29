import { randomUUID } from "node:crypto";
import { loadConfigFromDict } from "../config.js";
import type { BillingAutoRechargeProfile, BillingAutoRechargeStatus } from "./types/index.js";
import type {
  PaymentMethodInfo,
  PaymentProvider,
  SavedPaymentChargeQuote,
  SavedPaymentChargeResult,
} from "../providers/types.js";
import type { AutoRechargeBillingPort } from "./auto-recharge-port.js";
import { resolveAutoRechargeWindow } from "./policy-window.js";

export type AutoRechargeOutcome =
  | "not_configured"
  | "disabled"
  | "above_threshold"
  | "already_processing"
  | "limit_reached"
  | "submitted"
  | "action_required"
  | "failed";

export interface AutoRechargeProcessResult {
  outcome: AutoRechargeOutcome;
  charge?: SavedPaymentChargeResult | null;
}

interface ResolvedAutoRechargePolicy {
  enabled: true;
  threshold: number;
  topupKey: string;
  topupId: string;
  quantity: number;
  maxChargesPerWindow: number;
  windowUnit: "second" | "minute" | "hour" | "day" | "week" | "month" | "year";
  windowCount: number;
  windowAnchor: "calendar" | "plan_assignment" | "rolling";
  windowTimezone: string;
  windowStart: string;
  windowEnd: string;
  windowDays: number;
  productId: string;
}

export class AutoRechargeService {
  constructor(private readonly billing: AutoRechargeBillingPort) {}

  private async policy(provider: PaymentProvider): Promise<ResolvedAutoRechargePolicy | null> {
    const raw = await this.billing.getActiveBursarConfig();
    if (!raw) return null;
    const config = loadConfigFromDict(raw);
    const auto = config.commerce.autoRecharge;
    if (!auto) return null;

    const topupKey = auto.eligibleTopups[0];
    const topup = config.commerce.offers[topupKey];
    if (!topup || topup.type !== "topup") return null;
    const reference = topup.providers[provider.provider];
    if (!reference) return null;

    const productId =
      reference.type === "stripe_price"
        ? reference.priceId
        : reference.type === "dodo_product"
          ? reference.productId
          : reference.externalId;
    const resolvedTopup =
      reference.type === "stripe_price"
        ? await this.billing.resolveTopup(provider.provider, null, productId)
        : reference.type === "dodo_product"
          ? await this.billing.resolveTopup(provider.provider, productId, null)
          : await this.billing.resolveTopupByLookup(provider.provider, productId);
    if (!resolvedTopup) return null;

    const period = resolveAutoRechargeWindow(auto.limits.window);
    return {
      enabled: true,
      threshold: Number(auto.balanceBelow.default),
      topupKey,
      topupId: resolvedTopup.topupId,
      quantity: auto.quantity.default,
      maxChargesPerWindow: auto.limits.maxPurchases,
      windowUnit: period.unit,
      windowCount: period.count,
      windowAnchor: period.anchor,
      windowTimezone: period.timezone,
      windowStart: period.start,
      windowEnd: period.end,
      windowDays: period.durationDays,
      productId,
    };
  }

  private async paymentMethod(
    userId: string,
    provider: PaymentProvider,
  ): Promise<{ customerId: string; method: PaymentMethodInfo } | null> {
    const customer = await this.billing.getCustomerByUserId(userId, provider.provider);
    if (!customer) return null;
    if (!provider.listPaymentMethods) {
      throw new Error(`provider_capability_not_supported:listPaymentMethods:${provider.provider}`);
    }
    const methods = await provider.listPaymentMethods(customer.providerCustomerId);
    const method =
      (await provider.getDefaultPaymentMethod?.(customer.providerCustomerId)) ??
      methods.find((candidate) => candidate.isDefault) ??
      (methods.length === 1 ? methods[0] : null);
    return method ? { customerId: customer.providerCustomerId, method } : null;
  }

  async quote(input: {
    userId: string;
    provider: PaymentProvider;
  }): Promise<SavedPaymentChargeQuote | null> {
    const policy = await this.policy(input.provider);
    if (!policy || !input.provider.previewSavedPaymentCharge) return null;
    const payment = await this.paymentMethod(input.userId, input.provider);
    if (!payment) return null;
    return input.provider.previewSavedPaymentCharge({
      customerId: payment.customerId,
      paymentMethodId: payment.method.id,
      productId: policy.productId,
      quantity: policy.quantity,
      metadata: {},
      idempotencyKey: "auto-recharge-preview",
    });
  }

  async getStatus(input: {
    userId: string;
    provider: PaymentProvider;
  }): Promise<BillingAutoRechargeStatus | null> {
    const policy = await this.policy(input.provider);
    if (!policy) return null;
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    const payment = profile?.enabled
      ? await this.paymentMethod(input.userId, input.provider)
      : null;
    const quote = await this.quote(input);
    return {
      enabled: Boolean(profile?.enabled),
      state: profile?.enabled ? profile.state : "disabled",
      thresholdCredits: policy.threshold,
      topupKey: policy.topupKey,
      quantity: policy.quantity,
      maxRecharges: policy.maxChargesPerWindow,
      windowDays: policy.windowDays,
      windowStart: policy.windowStart,
      windowEnd: policy.windowEnd,
      rechargesInWindow: await this.billing.countAutoRechargeAttempts(
        input.userId,
        policy.windowStart,
      ),
      paymentMethodId: payment?.method.id ?? null,
      paymentMethodLast4: payment?.method.last4 ?? null,
      paymentMethodBrand: payment?.method.brand ?? null,
      suspendedReason: profile?.state === "paused" ? "auto_recharge_paused" : null,
      pendingAttemptId: null,
      quoteAmountMinor: quote?.amountMinor ?? null,
      quoteCurrency: quote?.currency ?? null,
    };
  }

  async enable(input: {
    userId: string;
    provider: PaymentProvider;
    balance: number;
    returnUrl?: string;
    consentReference?: string;
  }): Promise<BillingAutoRechargeStatus | null> {
    const policy = await this.policy(input.provider);
    if (!policy) throw new Error("auto_recharge_not_configured");
    const payment = await this.paymentMethod(input.userId, input.provider);
    if (!payment) throw new Error("payment_method_required");

    const profile: BillingAutoRechargeProfile = {
      userId: input.userId,
      enabled: true,
      state: "active",
      armed: true,
      provider: input.provider.provider,
      topupId: policy.topupId,
      quantity: policy.quantity,
      threshold: policy.threshold,
      maxChargesPerWindow: policy.maxChargesPerWindow,
      windowUnit: policy.windowUnit,
      windowCount: policy.windowCount,
      windowAnchor: policy.windowAnchor,
      windowTimezone: policy.windowTimezone,
    };
    await this.billing.upsertAutoRechargeProfile(profile);
    await this.processIfNeeded(input);
    return this.getStatus(input);
  }

  async disable(userId: string): Promise<void> {
    const profile = await this.billing.getAutoRechargeProfile(userId);
    if (!profile) return;
    await this.billing.upsertAutoRechargeProfile({
      ...profile,
      enabled: false,
      state: "disabled",
      armed: true,
    });
  }

  async retry(input: {
    userId: string;
    provider: PaymentProvider;
    balance: number;
    returnUrl?: string;
  }): Promise<AutoRechargeProcessResult> {
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    if (!profile?.enabled) throw new Error("auto_recharge_disabled");
    await this.billing.upsertAutoRechargeProfile({
      ...profile,
      state: "active",
      armed: true,
    });
    return this.processIfNeeded(input);
  }

  async processIfNeeded(input: {
    userId: string;
    provider: PaymentProvider;
    balance: number;
    returnUrl?: string;
  }): Promise<AutoRechargeProcessResult> {
    const policy = await this.policy(input.provider);
    if (!policy) return { outcome: "not_configured" };
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    if (!profile?.enabled || profile.state !== "active") return { outcome: "disabled" };
    if (input.balance >= policy.threshold) {
      if (profile.armed === false) {
        await this.billing.upsertAutoRechargeProfile({ ...profile, armed: true });
      }
      return { outcome: "above_threshold" };
    }

    const payment = await this.paymentMethod(input.userId, input.provider);
    if (!payment) return { outcome: "failed" };
    const attempt = await this.billing.claimAutoRechargeAttempt({
      userId: input.userId,
      idempotencyKey: `auto-recharge:${input.userId}:${randomUUID()}`,
    });
    if (!attempt) return { outcome: "limit_reached" };

    if (!input.provider.chargeSavedPaymentMethod) {
      throw new Error(
        `provider_capability_not_supported:chargeSavedPaymentMethod:${input.provider.provider}`,
      );
    }
    const charge = await input.provider.chargeSavedPaymentMethod({
      customerId: payment.customerId,
      paymentMethodId: payment.method.id,
      productId: policy.productId,
      quantity: policy.quantity,
      returnUrl: input.returnUrl,
      idempotencyKey: attempt.idempotencyKey,
      metadata: {
        auto_recharge_attempt_id: attempt.id,
        purpose: "credit_topup",
        userId: input.userId,
      },
    });
    if (charge.status === "requires_customer_action") {
      await this.billing.updateAutoRechargeAttempt({
        id: attempt.id,
        state: "submitted",
        providerAttemptId: charge.providerPaymentId ?? null,
      });
      await this.billing.upsertAutoRechargeProfile({ ...profile, state: "paused" });
      return { outcome: "action_required", charge };
    }
    if (charge.status === "succeeded" || charge.status === "processing") {
      await this.billing.updateAutoRechargeAttempt({
        id: attempt.id,
        state: "processing",
        providerAttemptId: charge.providerPaymentId ?? null,
      });
      return { outcome: "submitted", charge };
    }

    await this.billing.updateAutoRechargeAttempt({
      id: attempt.id,
      state: "failed",
      providerAttemptId: charge.providerPaymentId ?? null,
      failureCode: "payment_failed",
    });
    await this.billing.upsertAutoRechargeProfile({ ...profile, state: "paused" });
    return { outcome: "failed", charge };
  }
}
