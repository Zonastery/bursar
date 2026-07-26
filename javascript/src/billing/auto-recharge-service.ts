import type { BillingService } from "./billing-service.js";
import { loadConfigFromDict } from "../config.js";
import type { BillingAutoRechargeProfile, BillingAutoRechargeStatus } from "./billing-types.js";
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

interface ResolvedAutoRechargePolicy {
  enabled: true;
  thresholdCredits: number;
  topupKey: string;
  quantity: number;
  maxRecharges: number;
  windowDays: number;
  productId: string;
}

export class AutoRechargeService {
  constructor(private readonly billing: BillingService) {}

  private async policy(provider: PaymentProvider): Promise<ResolvedAutoRechargePolicy | null> {
    const raw = await this.billing.getActiveBursarConfig();
    if (!raw) return null;
    const config = loadConfigFromDict(raw);
    const payments = config.payments;
    const auto = payments?.autoRecharge;
    if (!payments || !auto) return null;
    const topupKey = auto.purchase.topup;
    const topup = payments.topups[topupKey];
    const productId = topup?.providers[provider.provider]?.lookup.value;
    if (!productId) return null;
    return {
      enabled: true,
      thresholdCredits: Number(auto.trigger.balanceBelow),
      topupKey,
      quantity: auto.purchase.quantity,
      maxRecharges: auto.limit.maxPurchases,
      windowDays: auto.limit.period.unit === "month" ? 0 : auto.limit.period.count,
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
    if (!customer) return null;
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    const paymentMethodId =
      profile?.paymentMethodId ??
      (await input.provider.getDefaultPaymentMethod?.(customer.providerCustomerId))?.id;
    if (!paymentMethodId) return null;
    try {
      return await input.provider.previewSavedPaymentCharge({
        customerId: customer.providerCustomerId,
        paymentMethodId,
        productId: policy.productId,
        quantity: policy.quantity,
        metadata: {},
        idempotencyKey: "auto-recharge-preview",
      });
    } catch {
      // A quote is informational; enabling consent must not depend on the
      // provider's preview endpoint being available.
      return null;
    }
  }

  async getStatus(input: {
    userId: string;
    provider: PaymentProvider;
  }): Promise<BillingAutoRechargeStatus | null> {
    const policy = await this.policy(input.provider);
    if (!policy) return null;
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    let methods: Awaited<ReturnType<PaymentProvider["listPaymentMethods"]>> = [];
    if (profile?.providerCustomerId) {
      try {
        methods = await input.provider.listPaymentMethods(profile.providerCustomerId);
      } catch {
        // The persisted profile remains authoritative after consent. Provider
        // method metadata is optional display data and must not hide status.
      }
    }
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
      quoteSnapshot: quote ? ({ ...quote } as Record<string, unknown>) : null,
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
    const now = new Date();
    const windowStart =
      policy.windowDays === 0
        ? new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString()
        : new Date(now.getTime() - policy.windowDays * 24 * 60 * 60 * 1000).toISOString();
    const policySnapshot = {
      thresholdCredits: policy.thresholdCredits,
      topupKey: policy.topupKey,
      quantity: policy.quantity,
    };
    const attempt = await this.billing.claimAutoRechargeAttempt({
      userId: input.userId,
      provider: input.provider.provider,
      topupKey: policy.topupKey,
      quantity: policy.quantity,
      windowStart,
      maxRecharges: policy.maxRecharges,
      triggerBalance: input.balance,
      policySnapshot,
      policyHash: JSON.stringify(policySnapshot),
      quotedAmountMinor: quote?.amountMinor ?? null,
      currency: quote?.currency ?? null,
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
