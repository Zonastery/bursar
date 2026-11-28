import { offerIsAvailable } from "../config/availability.js";
import { hasProviderBinding, resolveProviderId } from "../config/parse-commerce.js";
import { Decimal } from "decimal.js";
import { loadConfigFromDict } from "../config.js";
import {
  AutoRechargeDisabledError,
  AutoRechargeNotConfiguredError,
  PaymentMethodRequiredError,
  ProviderCapabilityNotSupportedError,
} from "../errors.js";
import type { BillingAutoRechargeProfile, BillingAutoRechargeStatus } from "./types/index.js";
import type {
  PaymentMethodInfo,
  PaymentProvider,
  SavedPaymentChargeQuote,
  SavedPaymentChargeResult,
} from "../providers/types.js";
import type { AutoRechargeBillingPort } from "./auto-recharge-port.js";
import { resolveAutoRechargeWindow } from "./policy-window.js";
import { persistedDiagnosticSummary } from "../shared/diagnostics.js";

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
  threshold: Decimal;
  topupKey: string;
  topupId: string;
  quantity: number;
  maxChargesPerWindow: number;
  windowUnit: "second" | "minute" | "hour" | "day" | "week" | "month" | "year";
  windowCount: number;
  windowAnchor: "calendar" | "rolling";
  windowTimezone: string;
  windowStart: string;
  windowEnd: string;
  productId: string;
}

export class AutoRechargeService {
  constructor(private readonly billing: AutoRechargeBillingPort) {}

  private async policy(
    provider: PaymentProvider,
    profile?: BillingAutoRechargeProfile | null,
  ): Promise<ResolvedAutoRechargePolicy | null> {
    const raw = await this.billing.getActiveCatalogDocument();
    if (!raw) return null;
    const config = loadConfigFromDict(raw, this.billing.providerEnvironment);
    const auto = config.commerce.autoRecharge;
    if (!auto) return null;

    const topupKey = profile?.enabled ? profile.topupKey : auto.eligibleTopups[0];
    if (!topupKey || !auto.eligibleTopups.includes(topupKey)) return null;
    const quantity = profile?.enabled ? profile.quantity : auto.quantity.default;
    if (quantity < auto.quantity.minimum || quantity > auto.quantity.maximum) return null;
    const topup = config.commerce.offers[topupKey];
    if (!topup || topup.type !== "topup" || !offerIsAvailable(topup.availability)) return null;
    if (
      profile?.enabled &&
      (profile.threshold.lt(auto.balanceBelow.minimum) ||
        profile.threshold.gt(auto.balanceBelow.maximum))
    )
      return null;
    if (topup.price.amountMinor > auto.limits.maxChargeMinor / quantity) return null;
    const reference = topup.providers[provider.provider];
    if (!reference) return null;
    const referenceId =
      reference.type === "stripe_price"
        ? reference.priceId
        : reference.type === "dodo_product"
          ? reference.productId
          : reference.externalId;
    if (
      typeof referenceId !== "string" &&
      (!this.billing.providerEnvironment ||
        !hasProviderBinding(reference, this.billing.providerEnvironment))
    )
      return null;

    const productId = resolveProviderId(referenceId, this.billing.providerEnvironment);
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
      threshold: profile?.enabled ? profile.threshold : auto.balanceBelow.default,
      topupKey,
      topupId: resolvedTopup.topupId,
      quantity,
      maxChargesPerWindow: auto.limits.maxPurchases,
      windowUnit: period.unit,
      windowCount: period.count,
      windowAnchor: period.anchor,
      windowTimezone: period.timezone,
      windowStart: period.start,
      windowEnd: period.end,
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
      throw new ProviderCapabilityNotSupportedError(provider.provider, "listPaymentMethods");
    }
    const methods = await provider.listPaymentMethods(customer.providerCustomerId);
    const method =
      methods.find((candidate) => candidate.isDefault) ??
      (methods.length === 1 ? methods[0] : null);
    return method ? { customerId: customer.providerCustomerId, method } : null;
  }

  private preview(
    provider: PaymentProvider,
    policy: ResolvedAutoRechargePolicy,
    payment: { customerId: string; method: PaymentMethodInfo },
  ): Promise<SavedPaymentChargeQuote> | null {
    if (!provider.previewSavedPaymentCharge) return null;
    return provider.previewSavedPaymentCharge({
      customerId: payment.customerId,
      paymentMethodId: payment.method.id,
      productId: policy.productId,
      quantity: policy.quantity,
      metadata: {},
      idempotencyKey: "auto-recharge-preview",
    });
  }

  async quote(input: {
    userId: string;
    provider: PaymentProvider;
  }): Promise<SavedPaymentChargeQuote | null> {
    const policy = await this.policy(input.provider);
    if (!policy) return null;
    const payment = await this.paymentMethod(input.userId, input.provider);
    if (!payment) return null;
    return this.preview(input.provider, policy, payment);
  }

  async getStatus(input: {
    userId: string;
    provider: PaymentProvider;
  }): Promise<BillingAutoRechargeStatus | null> {
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    const policy = await this.policy(input.provider, profile);
    if (!policy) return null;
    const window = await this.billing.getAutoRechargeOperationalWindow(input.userId);
    if (!window) return null;
    const payment = profile?.enabled
      ? await this.paymentMethod(input.userId, input.provider)
      : null;
    const quote = payment ? await this.preview(input.provider, policy, payment) : null;
    return {
      enabled: Boolean(profile?.enabled),
      state: profile?.enabled ? profile.state : "disabled",
      thresholdCredits: policy.threshold,
      topupKey: policy.topupKey,
      quantity: policy.quantity,
      maxRecharges: window.maxRecharges,
      windowStart: window.windowStart,
      windowEnd: window.windowEnd,
      rechargesInWindow: window.count,
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
    balance: Decimal;
    returnUrl?: string;
  }): Promise<BillingAutoRechargeStatus | null> {
    const policy = await this.policy(input.provider);
    if (!policy) throw new AutoRechargeNotConfiguredError();
    const payment = await this.paymentMethod(input.userId, input.provider);
    if (!payment) throw new PaymentMethodRequiredError();

    const profile: BillingAutoRechargeProfile = {
      userId: input.userId,
      enabled: true,
      state: "active",
      armed: true,
      provider: input.provider.provider,
      topupId: policy.topupId,
      topupKey: policy.topupKey,
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
    balance: Decimal;
    returnUrl?: string;
  }): Promise<AutoRechargeProcessResult> {
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    if (!profile?.enabled) throw new AutoRechargeDisabledError();
    const policy = await this.policy(input.provider, profile);
    if (!policy || profile.provider !== input.provider.provider)
      return { outcome: "not_configured" };
    if (!(await this.billing.resumeAutoRechargeProfile(profile)))
      return { outcome: "not_configured" };
    return this.processIfNeeded(input);
  }

  async processIfNeeded(input: {
    userId: string;
    provider: PaymentProvider;
    balance: Decimal;
    returnUrl?: string;
  }): Promise<AutoRechargeProcessResult> {
    const profile = await this.billing.getAutoRechargeProfile(input.userId);
    if (!profile?.enabled || profile.state !== "active") return { outcome: "disabled" };
    const policy = await this.policy(input.provider, profile);
    if (!policy) return { outcome: "not_configured" };
    if (profile.provider !== input.provider.provider) return { outcome: "not_configured" };
    if (input.balance.gte(policy.threshold)) {
      return { outcome: "above_threshold" };
    }

    const payment = await this.paymentMethod(input.userId, input.provider);
    if (!payment) return { outcome: "failed" };
    if (!input.provider.chargeSavedPaymentMethod) {
      throw new ProviderCapabilityNotSupportedError(
        input.provider.provider,
        "chargeSavedPaymentMethod",
      );
    }
    const idempotencyKey = `auto-recharge:${input.userId}:${globalThis.crypto.randomUUID()}`;
    const attempt = await this.billing.claimAutoRechargeAttempt({
      userId: input.userId,
      idempotencyKey,
      expectedProductId: policy.productId,
      expectedQuantity: policy.quantity,
      expectedTopupKey: policy.topupKey,
    });
    if (!attempt) return { outcome: "limit_reached" };
    // A transport error leaves the provider outcome unknown. The database
    // returns that in-flight attempt on the next claim; replay the provider
    // request with its original key so a committed charge cannot be duplicated.
    if (attempt.idempotencyKey !== idempotencyKey && attempt.state !== "unknown") {
      return { outcome: "already_processing" };
    }

    let charge: SavedPaymentChargeResult;
    try {
      charge = await input.provider.chargeSavedPaymentMethod({
        customerId: payment.customerId,
        paymentMethodId: payment.method.id,
        productId: policy.productId,
        quantity: policy.quantity,
        returnUrl: input.returnUrl,
        idempotencyKey: attempt.idempotencyKey,
        metadata: {
          auto_recharge_attempt_id: attempt.id,
          purpose: "credit_topup",
          bursar_account_id: input.userId,
        },
      });
    } catch (cause) {
      await this.billing.updateAutoRechargeAttempt({
        id: attempt.id,
        state: "unknown",
        failureCode: "provider_request_failed",
        failureMessage: persistedDiagnosticSummary(cause, "auto_recharge_provider_failed"),
      });
      throw cause;
    }
    if (charge.status === "requires_customer_action") {
      await this.billing.updateAutoRechargeAttempt({
        id: attempt.id,
        state: "action_required",
        providerAttemptId: charge.providerPaymentId ?? null,
      });
      return { outcome: "action_required", charge };
    }
    if (
      charge.status === "succeeded" ||
      charge.status === "processing" ||
      charge.status === "requires_merchant_action" ||
      charge.status === "requires_confirmation" ||
      charge.status === "requires_capture" ||
      charge.status === "partially_captured" ||
      charge.status === "partially_captured_and_capturable"
    ) {
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
    return { outcome: "failed", charge };
  }
}
