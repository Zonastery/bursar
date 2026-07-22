import type { BillingService } from "./billing-service.js";
import type {
  BillingAutoRechargeConfig,
  BillingAutoRechargeProfile,
  BillingAutoRechargeStatus,
} from "./billing-types.js";
import type {
  PaymentProvider,
  SavedPaymentChargeQuote,
  SavedPaymentChargeResult,
} from "../providers/types.js";

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

export class AutoRechargeService {
  constructor(private readonly billing: BillingService) {}

  private async policy(
    provider: PaymentProvider,
  ): Promise<(BillingAutoRechargeConfig & { productId: string }) | null> {
    const raw = (await this.billing.getActiveBursarConfig()) ?? {};
    const billing = (raw.billing ?? {}) as Record<string, unknown>;
    const auto = (billing.auto_recharge ?? billing.autoRecharge ?? {}) as Record<string, unknown>;
    if (auto.enabled === false || Object.keys(auto).length === 0) return null;
    const defaultPolicy = (auto.default_policy ?? auto.defaultPolicy ?? {}) as Record<
      string,
      unknown
    >;
    const trigger = (defaultPolicy.trigger ?? {}) as Record<string, unknown>;
    const topupPolicy = (defaultPolicy.topup ?? {}) as Record<string, unknown>;
    const limit = (defaultPolicy.limit ?? {}) as Record<string, unknown>;
    const topupKey = String(topupPolicy.key ?? auto.topup_key ?? auto.topupKey ?? "default");
    const topups = (billing.topups ?? {}) as Record<string, unknown>;
    const topup = (topups[topupKey] ?? {}) as Record<string, unknown>;
    const refs = (topup.providers ?? {}) as Record<string, unknown>;
    const ref = (refs[provider.provider] ?? {}) as Record<string, unknown>;
    const productId = String(ref.price_id ?? ref.priceId ?? ref.product_id ?? ref.productId ?? "");
    if (!productId) return null;
    return {
      enabled: auto.enabled !== false,
      thresholdCredits: Number(
        trigger.threshold_credits ??
          trigger.thresholdCredits ??
          auto.threshold_credits ??
          auto.thresholdCredits ??
          0,
      ),
      topupKey,
      quantity: Number(topupPolicy.quantity ?? auto.quantity ?? 1),
      maxRecharges: Number(
        limit.max_charges ?? limit.maxCharges ?? auto.max_recharges ?? auto.maxRecharges ?? 3,
      ),
      windowDays:
        String(limit.period ?? "") === "calendar_month"
          ? 0
          : Number(
              limit.rolling_days ?? limit.rollingDays ?? auto.window_days ?? auto.windowDays ?? 30,
            ),
      productId,
    };
  }

  async quote(input: {
    userId: string;
    provider: PaymentProvider;
  }): Promise<SavedPaymentChargeQuote | null> {
    const policy = await this.policy(input.provider);
    if (!policy || !input.provider.previewSavedPaymentCharge) return null;
    const customer = await this.billing.getCustomerByUserId(input.userId, input.provider.provider);
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    const paymentMethodId =
      profile?.paymentMethodId ??
      (await input.provider.getDefaultPaymentMethod?.(customer?.providerCustomerId ?? ""))?.id;
    if (!customer || !paymentMethodId) return null;
    return input.provider.previewSavedPaymentCharge({
      customerId: customer.providerCustomerId,
      paymentMethodId,
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
    const methods = profile?.providerCustomerId
      ? await input.provider.listPaymentMethods(profile.providerCustomerId)
      : [];
    const method = methods.find((item) => item.id === profile?.paymentMethodId);
    const state = profile?.enabled ? profile.state : "disabled";
    const quote = await this.quote(input);
    return {
      enabled: Boolean(profile?.enabled),
      state,
      thresholdCredits: policy.thresholdCredits,
      topupKey: policy.topupKey,
      quantity: policy.quantity,
      maxRecharges: policy.maxRecharges,
      windowDays: policy.windowDays,
      rechargesInWindow: await this.billing.countAutoRechargeAttempts(
        input.userId,
        policy.windowDays,
      ),
      paymentMethodId: profile?.paymentMethodId ?? null,
      paymentMethodLast4: method?.last4 ?? null,
      paymentMethodBrand: method?.brand ?? null,
      suspendedReason: profile?.suspendedReason ?? null,
      pendingAttemptId: null,
      quoteAmountMinor: quote?.amountMinor ?? null,
      quoteCurrency: quote?.currency ?? null,
    };
  }

  async enable(input: {
    userId: string;
    provider: PaymentProvider;
    balance: number;
    returnUrl: string;
    consentReference?: string;
  }): Promise<BillingAutoRechargeStatus | null> {
    const policy = await this.policy(input.provider);
    if (!policy) throw new Error("auto_recharge_not_configured");
    const customer = await this.billing.getCustomerByUserId(input.userId, input.provider.provider);
    if (!customer) throw new Error("payment_method_required");
    const method =
      (await input.provider.getDefaultPaymentMethod?.(customer.providerCustomerId)) ??
      ((await input.provider.listPaymentMethods(customer.providerCustomerId)).length === 1
        ? (await input.provider.listPaymentMethods(customer.providerCustomerId))[0]
        : null);
    if (!method) throw new Error("payment_method_selection_required");
    const quote = await this.quote({ userId: input.userId, provider: input.provider });
    const profile: BillingAutoRechargeProfile = {
      userId: input.userId,
      enabled: true,
      state: "active",
      provider: input.provider.provider,
      providerCustomerId: customer.providerCustomerId,
      paymentMethodId: method.id,
      suspendedReason: null,
      consentedAt: new Date().toISOString(),
      policySnapshot: {
        thresholdCredits: policy.thresholdCredits,
        topupKey: policy.topupKey,
        quantity: policy.quantity,
      },
      policyHash: JSON.stringify({
        thresholdCredits: policy.thresholdCredits,
        topupKey: policy.topupKey,
        quantity: policy.quantity,
      }),
      quoteSnapshot: quote ?? null,
      consentReference: input.consentReference ?? null,
      armed: true,
    };
    await this.billing.upsertAutoRechargeProfile(profile);
    await this.processIfNeeded(input);
    return this.getStatus(input);
  }

  async disable(userId: string): Promise<void> {
    const profile = await this.billing.getAutoRechargeProfile(userId);
    await this.billing.upsertAutoRechargeProfile({
      userId,
      enabled: false,
      state: "disabled",
      provider: profile?.provider ?? null,
      providerCustomerId: profile?.providerCustomerId ?? null,
      paymentMethodId: profile?.paymentMethodId ?? null,
      suspendedReason: null,
      consentedAt: profile?.consentedAt ?? null,
    });
  }

  async retry(input: {
    userId: string;
    provider: PaymentProvider;
    balance: number;
    returnUrl: string;
  }): Promise<AutoRechargeProcessResult> {
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    if (!profile?.enabled) throw new Error("auto_recharge_disabled");
    await this.billing.upsertAutoRechargeProfile({
      ...profile,
      state: "active",
      armed: true,
      suspendedReason: null,
    });
    return this.processIfNeeded(input);
  }

  async processIfNeeded(input: {
    userId: string;
    provider: PaymentProvider;
    balance: number;
    returnUrl: string;
  }): Promise<AutoRechargeProcessResult> {
    const policy = await this.policy(input.provider);
    if (!policy) return { outcome: "not_configured" };
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    if (!profile?.enabled || profile.state !== "active") return { outcome: "disabled" };
    if (input.balance >= policy.thresholdCredits) {
      if (profile.armed === false)
        await this.billing.upsertAutoRechargeProfile({ ...profile, armed: true });
      return { outcome: "above_threshold" };
    }
    const quote = await this.quote({ userId: input.userId, provider: input.provider });
    const attempt = await this.billing.claimAutoRechargeAttempt({
      userId: input.userId,
      provider: input.provider.provider,
      topupKey: policy.topupKey,
      quantity: policy.quantity,
      maxRecharges: policy.maxRecharges,
      windowDays: policy.windowDays,
    });
    if (!attempt) return { outcome: "limit_reached" };
    const charge = await input.provider.chargeSavedPaymentMethod({
      customerId: profile.providerCustomerId!,
      paymentMethodId: profile.paymentMethodId!,
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
        state: "action_required",
        providerPaymentId: charge.providerPaymentId ?? null,
        actionUrl: charge.actionUrl ?? null,
      });
      await this.billing.upsertAutoRechargeProfile({
        ...profile,
        state: "suspended",
        suspendedReason: "customer_action_required",
      });
      return { outcome: "action_required", charge };
    }
    if (charge.status === "succeeded" || charge.status === "processing") {
      await this.billing.updateAutoRechargeAttempt({
        id: attempt.id,
        state: "processing",
        providerPaymentId: charge.providerPaymentId ?? null,
      });
      return { outcome: "submitted", charge };
    }
    await this.billing.updateAutoRechargeAttempt({
      id: attempt.id,
      state: "failed",
      providerPaymentId: charge.providerPaymentId ?? null,
      failureCode: "payment_failed",
    });
    await this.billing.upsertAutoRechargeProfile({
      ...profile,
      state: "suspended",
      suspendedReason: "payment_failed",
    });
    return { outcome: "failed", charge };
  }
}
