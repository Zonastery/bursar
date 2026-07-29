import { createHash } from "node:crypto";

import type { BillingCapability, BillingEventSink } from "../billing/contracts.js";
import type { BillingPreferences, BillingSubscriptionState } from "../billing/types/index.js";
import { loadConfigFromDict } from "../config.js";
import type {
  CommerceOffer,
  ParsedBursarConfig,
  ProviderReference,
  SubscriptionChangePolicy,
  SubscriptionOffer,
} from "../config/types.js";
import type { CreditsService as CreditsServiceImpl } from "../credits/service.js";
import type { LedgerEntry } from "../credits/types/index.js";
import type {
  ChangePlanPreview,
  PaymentMethodInfo,
  PaymentProvider,
  PreviewChangePlanParams,
} from "../providers/types.js";
import { normalizeLogger } from "../shared/logger.js";
import {
  ActiveSubscriptionError,
  CheckoutCompletedError,
  CheckoutConflictError,
  CommerceResourceNotFoundError,
  CoreBillingDataUnavailableError,
  InvalidOfferQuantityError,
  MissingPaymentMethodError,
  ProviderCapabilityNotSupportedError,
  QuoteChangedError,
  UnknownOfferError,
} from "./errors.js";
import { CommerceProviderRegistry } from "./provider-registry.js";
import { classifySubscriptionChange } from "./plan-change.js";
import type {
  AccountCommerceOverview,
  AutoRechargeInput,
  BillingDocumentRef,
  CommerceAutoRecharge,
  CommerceCheckoutKind,
  CommerceOptions,
  CommerceWebhookInput,
  CommerceWebhookResult,
  ConfirmPlanChangeInput,
  ConfirmPlanChangeResult,
  CreateCheckoutInput,
  CreateCheckoutResult,
  GetInvoiceLinkInput,
  PlanChangeClassification,
  PlanChangePreviewResult,
  PortalSessionInput,
  PreferencePatch,
  PreviewPlanChangeInput,
  SubscriptionCommandResult,
} from "./types.js";

const DEFAULT_CHECKOUT_INTENT_TTL_MS = 24 * 60 * 60 * 1_000;

const DEFAULT_PREFERENCES = {
  autoRecharge: false,
  overageProtection: true,
  emailNotifications: true,
  usageAlerts: true,
  invoiceReminders: false,
} as const;

const TERMINAL_CHECKOUT_STATUSES = new Set(["failed", "cancelled", "requires_payment_method"]);

function externalId(reference: ProviderReference): string {
  switch (reference.type) {
    case "stripe_price":
      return reference.priceId;
    case "dodo_product":
      return reference.productId;
    case "custom_object":
      return reference.externalId;
  }
}

function checkoutRedirectUrl(template: string, intentId: string): string {
  return template.replaceAll("{intentId}", encodeURIComponent(intentId));
}

function quoteFingerprint(preview: ChangePlanPreview): string {
  const amount = (value: unknown): number | null => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  };
  const financialFields = {
    totalAmount: amount(preview.totalAmount),
    settlementAmount: amount(preview.settlementAmount),
    currency: preview.currency.toUpperCase(),
    recurringAmount: amount(preview.recurringAmount),
    recurringCurrency: preview.recurringCurrency?.toUpperCase() ?? null,
    taxAmount: amount(preview.taxAmount),
    customerCredits: amount(preview.customerCredits),
    lineItems: preview.lineItems.map((item) => ({
      productId: item.productId,
      unitPrice: amount(item.unitPrice),
      quantity: amount(item.quantity),
      prorationFactor: amount(item.prorationFactor),
      currency: item.currency.toUpperCase(),
      tax: amount(item.tax),
      subtotal: amount(item.subtotal),
    })),
  };
  return createHash("sha256").update(JSON.stringify(financialFields)).digest("hex");
}

function providerPlanChangeParams(
  policy: SubscriptionChangePolicy,
): Pick<PreviewChangePlanParams, "effectiveAt" | "prorationBillingMode"> {
  return {
    effectiveAt: policy.effective === "renewal" ? "next_billing_date" : "immediately",
    prorationBillingMode: policy.proration === "none" ? "do_not_bill" : "prorated_immediately",
  };
}

interface ResolvedOffer {
  config: ParsedBursarConfig;
  offerKey: string;
  offer: CommerceOffer;
}

interface PlanChangeContext {
  subscription: BillingSubscriptionState;
  provider: PaymentProvider;
  targetOfferId: string;
  targetOffer: SubscriptionOffer;
  targetProductId: string;
  targetInterval: "month" | "year";
  classification: PlanChangeClassification;
  policy?: SubscriptionChangePolicy;
}

class CommerceAutoRechargeService implements CommerceAutoRecharge {
  constructor(private readonly commerce: CommerceService) {}

  async getStatus(input: Pick<AutoRechargeInput, "accountId">) {
    const provider = await this.commerce.providerForAccount(input.accountId);
    return this.commerce.billing.autoRecharge.getStatus({
      userId: input.accountId,
      provider,
    });
  }

  async enable(input: AutoRechargeInput) {
    const [provider, balance] = await Promise.all([
      this.commerce.providerForAccount(input.accountId),
      this.commerce.credits.getBalance(input.accountId),
    ]);
    try {
      return await this.commerce.billing.autoRecharge.enable({
        userId: input.accountId,
        provider,
        balance: Number(balance.balance),
        returnUrl: input.returnUrl,
      });
    } catch (error) {
      if (error instanceof Error && error.message.includes("payment_method")) {
        throw new MissingPaymentMethodError();
      }
      throw error;
    }
  }

  async disable(input: Pick<AutoRechargeInput, "accountId">): Promise<void> {
    await this.commerce.billing.autoRecharge.disable(input.accountId);
  }

  async retry(input: AutoRechargeInput) {
    const [provider, balance] = await Promise.all([
      this.commerce.providerForAccount(input.accountId),
      this.commerce.credits.getBalance(input.accountId),
    ]);
    try {
      await this.commerce.billing.autoRecharge.retry({
        userId: input.accountId,
        provider,
        balance: Number(balance.balance),
        returnUrl: input.returnUrl,
      });
      return this.commerce.billing.autoRecharge.getStatus({
        userId: input.accountId,
        provider,
      });
    } catch (error) {
      if (error instanceof Error && error.message.includes("payment_method")) {
        throw new MissingPaymentMethodError();
      }
      throw error;
    }
  }

  async processIfNeeded(input: AutoRechargeInput) {
    const profile = await this.commerce.billing.getAutoRechargeProfile(input.accountId);
    if (!profile?.enabled || profile.state !== "active") {
      return { outcome: "disabled" as const };
    }
    const [provider, balance] = await Promise.all([
      profile.provider
        ? this.commerce.providers.get(profile.provider)
        : this.commerce.providerForAccount(input.accountId),
      this.commerce.credits.getBalance(input.accountId),
    ]);
    return this.commerce.billing.autoRecharge.processIfNeeded({
      userId: input.accountId,
      provider,
      balance: Number(balance.balance),
      returnUrl: input.returnUrl,
    });
  }
}

/** Framework-independent catalog, billing-state, and provider coordinator. */
export class CommerceService {
  readonly autoRecharge: CommerceAutoRecharge;
  readonly providers: CommerceProviderRegistry;
  readonly logger: ReturnType<typeof normalizeLogger>;

  constructor(
    readonly billing: BillingCapability,
    readonly credits: Pick<CreditsServiceImpl, keyof CreditsServiceImpl>,
    eventSink: BillingEventSink,
    readonly options: CommerceOptions,
  ) {
    this.logger = normalizeLogger(options.logger);
    this.providers = new CommerceProviderRegistry(options, {
      eventSink,
      identityResolver: options.identityResolver,
    });
    this.autoRecharge = new CommerceAutoRechargeService(this);
  }

  /** Exposed for application composition and compatibility, not provider selection policy. */
  getProvider(providerName: string): Promise<PaymentProvider> {
    return this.providers.get(providerName);
  }

  clearProviderCache(): void {
    this.providers.clear();
  }

  private async activeConfig(): Promise<ParsedBursarConfig> {
    const raw = await this.billing.getActiveBursarConfig();
    if (!raw) {
      throw new CoreBillingDataUnavailableError("The active commerce catalog is unavailable");
    }
    try {
      return loadConfigFromDict(raw);
    } catch (error) {
      throw new CoreBillingDataUnavailableError("The active commerce catalog is invalid", error);
    }
  }

  private async resolveOffer(input: {
    offerKey: string;
    type?: CommerceCheckoutKind;
  }): Promise<ResolvedOffer> {
    const config = await this.activeConfig();
    const offer = config.commerce.offers[input.offerKey];
    if (!offer) throw new UnknownOfferError(`Unknown offer '${input.offerKey}'`);
    this.assertOfferType(offer, input.type);
    return { config, offerKey: input.offerKey, offer };
  }

  private assertOfferType(offer: CommerceOffer, type?: CommerceCheckoutKind): void {
    if (!type) return;
    const expected = type === "credit_pack" ? "topup" : "subscription";
    if (offer.type !== expected) {
      throw new UnknownOfferError(`Offer is not a ${type} offer`);
    }
  }

  private quantity(offer: CommerceOffer, requested?: number): number {
    if (offer.type === "subscription") {
      if (requested != null && requested !== 1) {
        throw new InvalidOfferQuantityError("Subscription quantity must be 1", 1, 1);
      }
      return 1;
    }
    const quantity = requested ?? offer.quantity.default;
    if (!Number.isInteger(quantity) || quantity < offer.quantity.minimum) {
      throw new InvalidOfferQuantityError(
        `Minimum quantity is ${offer.quantity.minimum}`,
        offer.quantity.minimum,
        offer.quantity.maximum,
      );
    }
    if (quantity > offer.quantity.maximum) {
      throw new InvalidOfferQuantityError(
        `Maximum quantity is ${offer.quantity.maximum}`,
        offer.quantity.minimum,
        offer.quantity.maximum,
      );
    }
    return quantity;
  }

  async providerForAccount(accountId: string, offer?: CommerceOffer): Promise<PaymentProvider> {
    const subscription = await this.billing.getUserSubscription(accountId);
    const customer = await this.billing.getCustomerByUserId(accountId, subscription?.provider);
    return this.providers.select({
      current: subscription?.provider ?? customer?.provider,
      offer,
    });
  }

  async createCheckout(input: CreateCheckoutInput): Promise<CreateCheckoutResult> {
    const resolved = await this.resolveOffer(input);
    const quantity = this.quantity(resolved.offer, input.quantity);
    const accountState = input.accountId
      ? await Promise.all([
          this.billing.getBlockingSubscription(input.accountId),
          this.billing.getCustomerByUserId(input.accountId),
        ])
      : ([null, null] as const);
    if (resolved.offer.type === "subscription" && accountState[0]) {
      throw new ActiveSubscriptionError();
    }

    const provider = await this.providers.select({
      requested: input.provider,
      current: accountState[1]?.provider,
      offer: resolved.offer,
    });
    const reference = resolved.offer.providers[provider.provider];
    if (!reference) throw new UnknownOfferError("Offer has no reference for the selected provider");
    const productId = externalId(reference);
    const customer = input.accountId
      ? await this.billing.getCustomerByUserId(input.accountId, provider.provider)
      : null;

    const metadata: Record<string, string> = {
      ...(input.metadata ?? {}),
      ...(input.accountId ? { userId: input.accountId } : {}),
    };
    if (resolved.offer.type === "subscription") {
      metadata.plan_slug = resolved.offer.plan;
      metadata.billing_interval = resolved.offer.billingInterval.unit;
    } else {
      metadata.credits = resolved.offer.creditsPerUnit.mul(quantity).toString();
      metadata.quantity = String(quantity);
    }

    const requestDigest = createHash("sha256")
      .update(
        JSON.stringify({
          checkoutKind: resolved.offer.type,
          offerKey: resolved.offerKey,
          provider: provider.provider,
          quantity,
        }),
      )
      .digest("hex");
    const expiresAt = () =>
      new Date(
        Date.now() + (this.options.checkoutIntentTtlMs ?? DEFAULT_CHECKOUT_INTENT_TTL_MS),
      ).toISOString();
    const createIntent = () =>
      this.billing.createOrGetCheckoutIntent({
        subjectId: input.subjectId,
        provider: provider.provider,
        checkoutKind: resolved.offer.type === "subscription" ? "subscription" : "credit_topup",
        productKey: resolved.offerKey,
        requestDigest,
        expiresAt: expiresAt(),
      });

    let intent = await createIntent();
    if (intent.requestDigest !== requestDigest) {
      throw new CheckoutConflictError("A checkout is already in progress for a different offer");
    }
    if (intent.checkoutUrl) {
      const locallyExpired = new Date(intent.expiresAt).getTime() <= Date.now();
      if (!locallyExpired && (!intent.providerSessionId || !provider.getCheckoutSessionStatus)) {
        throw new CheckoutConflictError(
          "A checkout is already in progress; continue it in the existing checkout window",
        );
      }
      const providerState = locallyExpired
        ? null
        : await provider.getCheckoutSessionStatus!(intent.providerSessionId!);
      if (providerState?.paymentStatus === "succeeded") {
        await this.billing.updateCheckoutIntent(intent.id, { status: "completed" });
        throw new CheckoutCompletedError();
      }
      if (
        !locallyExpired &&
        providerState !== null &&
        !TERMINAL_CHECKOUT_STATUSES.has(providerState.paymentStatus ?? "")
      ) {
        throw new CheckoutConflictError(
          "A checkout is already in progress; continue it in the existing checkout window",
        );
      }
      await this.billing.updateCheckoutIntent(intent.id, {
        status: providerState === null ? "expired" : "failed",
      });
      intent = await createIntent();
    }

    try {
      const session = await provider.createCheckoutSession({
        ...(input.accountId ? { userId: input.accountId } : {}),
        ...(customer?.providerCustomerId ? { customerId: customer.providerCustomerId } : {}),
        ...(input.email ? { email: input.email } : {}),
        productId,
        type: resolved.offer.type === "subscription" ? "subscription" : "credit_pack",
        quantity,
        returnUrl: checkoutRedirectUrl(input.returnUrl, intent.id),
        cancelUrl: checkoutRedirectUrl(input.cancelUrl, intent.id),
        metadata: { ...metadata, checkout_intent_id: intent.id },
        idempotencyKey: input.operationKey,
      });
      await this.billing.updateCheckoutIntent(intent.id, {
        providerSessionId: session.providerSessionId,
        checkoutUrl: session.url,
      });
      if (
        input.accountId &&
        session.customerId &&
        session.customerId !== customer?.providerCustomerId
      ) {
        await this.billing.upsertCustomer(
          provider.provider,
          session.customerId,
          input.accountId,
          input.email ?? null,
        );
      }
      return {
        intentId: intent.id,
        url: session.url,
        provider: provider.provider,
        offerKey: resolved.offerKey,
      };
    } catch (error) {
      await this.billing.updateCheckoutIntent(intent.id, { status: "failed" });
      throw error;
    }
  }

  async getCheckoutStatus(input: { intentId: string; subjectId: string }) {
    const intent = await this.billing.getCheckoutIntent(input.intentId, input.subjectId);
    if (!intent) throw new CommerceResourceNotFoundError("Checkout intent not found");
    const expired = intent.status === "open" && new Date(intent.expiresAt).getTime() <= Date.now();
    return {
      intentId: intent.id,
      status: expired
        ? ("expired" as const)
        : intent.status === "open"
          ? ("pending" as const)
          : intent.status === "completed"
            ? ("succeeded" as const)
            : intent.status,
    };
  }

  async cancelSubscription(input: {
    accountId: string;
    operationKey: string;
  }): Promise<SubscriptionCommandResult> {
    const subscription = await this.billing.getUserSubscription(input.accountId);
    if (!subscription?.providerSubscriptionId) {
      throw new CommerceResourceNotFoundError("No active subscription found");
    }
    if (!["active", "trialing", "past_due"].includes(subscription.status ?? "")) {
      return { ok: true };
    }
    const provider = await this.providers.get(subscription.provider);
    if (!provider.cancelSubscription) {
      throw new ProviderCapabilityNotSupportedError(provider.provider, "cancelSubscription");
    }
    await provider.cancelSubscription(subscription.providerSubscriptionId, input.operationKey);
    await this.billing.ingestBillingEvent({
      provider: subscription.provider,
      eventId: `cancel_${input.accountId}_${input.operationKey}`,
      eventType: "subscription.cancellation_scheduled",
      occurredAt: new Date().toISOString(),
      userId: input.accountId,
      customer: { providerCustomerId: subscription.providerCustomerId },
      subscription: {
        providerSubscriptionId: subscription.providerSubscriptionId,
        cancelAtPeriodEnd: true,
      },
    });
    return { ok: true, pending: true };
  }

  async reactivateSubscription(input: {
    accountId: string;
    operationKey: string;
  }): Promise<SubscriptionCommandResult> {
    const subscription = await this.billing.getUserSubscription(input.accountId);
    if (!subscription?.providerSubscriptionId) {
      throw new CommerceResourceNotFoundError("No subscription found");
    }
    if (
      (subscription.status === "active" && !subscription.cancelAtPeriodEnd) ||
      (!subscription.cancelAtPeriodEnd && subscription.status !== "canceled")
    ) {
      return { ok: true };
    }
    const provider = await this.providers.get(subscription.provider);
    if (!provider.reactivateSubscription) {
      throw new ProviderCapabilityNotSupportedError(provider.provider, "reactivateSubscription");
    }
    await provider.reactivateSubscription(subscription.providerSubscriptionId, input.operationKey);
    await this.billing.ingestBillingEvent({
      provider: subscription.provider,
      eventId: `reactivate_${input.accountId}_${input.operationKey}`,
      eventType: "subscription.cancellation_unscheduled",
      occurredAt: new Date().toISOString(),
      userId: input.accountId,
      customer: { providerCustomerId: subscription.providerCustomerId },
      subscription: {
        providerSubscriptionId: subscription.providerSubscriptionId,
        cancelAtPeriodEnd: false,
      },
    });
    return { ok: true, pending: true };
  }

  private async planChangeContext(input: PreviewPlanChangeInput): Promise<PlanChangeContext> {
    const [subscription, entitlement, resolved] = await Promise.all([
      this.billing.getActiveSubscription(input.accountId),
      this.credits.getUserPlan(input.accountId),
      this.resolveOffer({
        offerKey: input.offerKey,
        type: "subscription",
      }),
    ]);
    if (!subscription?.providerSubscriptionId) {
      throw new CommerceResourceNotFoundError("No active subscription found");
    }
    if (resolved.offer.type !== "subscription") throw new UnknownOfferError();
    const provider = await this.providers.select({
      requested: subscription.provider,
      current: subscription.provider,
      offer: resolved.offer,
    });
    const reference = resolved.offer.providers[provider.provider];
    if (!reference) throw new UnknownOfferError("Target offer is unavailable from the provider");
    const persistedOffer =
      reference.type === "stripe_price"
        ? await this.billing.resolveOffer(provider.provider, null, reference.priceId)
        : reference.type === "dodo_product"
          ? await this.billing.resolveOffer(provider.provider, reference.productId, null)
          : await this.billing.resolveOfferByLookup(provider.provider, reference.externalId);
    if (!persistedOffer) {
      throw new CommerceResourceNotFoundError(
        "Target offer is not present in persisted billing state",
      );
    }
    const currentPlan = entitlement.planKey ?? subscription.plan;
    if (!currentPlan) {
      throw new CommerceResourceNotFoundError("Current subscription plan is unknown");
    }
    const { classification, targetInterval, policy } = classifySubscriptionChange(
      resolved.config,
      currentPlan,
      subscription.interval,
      resolved.offer,
    );
    return {
      subscription,
      provider,
      targetOfferId: persistedOffer.offerId,
      targetOffer: resolved.offer,
      targetProductId: externalId(reference),
      targetInterval,
      classification,
      policy,
    };
  }

  private async refreshPlanPreview(context: PlanChangeContext): Promise<ChangePlanPreview> {
    if (!context.provider.previewChangePlan) {
      throw new ProviderCapabilityNotSupportedError(context.provider.provider, "previewChangePlan");
    }
    return context.provider.previewChangePlan({
      providerSubscriptionId: context.subscription.providerSubscriptionId,
      productId: context.targetProductId,
      ...providerPlanChangeParams(context.policy!),
    });
  }

  async previewPlanChange(input: PreviewPlanChangeInput): Promise<PlanChangePreviewResult> {
    const context = await this.planChangeContext(input);
    if (context.classification === "unchanged") {
      return {
        unchanged: true,
        classification: "unchanged",
        scheduled: false,
        planId: context.targetOffer.plan,
        interval: context.targetInterval,
      };
    }
    const preview = await this.refreshPlanPreview(context);
    return {
      unchanged: false,
      classification: context.classification,
      scheduled: context.policy!.effective === "renewal",
      planId: context.targetOffer.plan,
      interval: context.targetInterval,
      preview,
      quoteFingerprint: quoteFingerprint(preview),
    };
  }

  async confirmPlanChange(input: ConfirmPlanChangeInput): Promise<ConfirmPlanChangeResult> {
    const context = await this.planChangeContext(input);
    if (context.classification === "unchanged") {
      return {
        success: true,
        unchanged: true,
        planId: context.targetOffer.plan,
        interval: context.targetInterval,
      };
    }
    const preview = await this.refreshPlanPreview(context);
    const fingerprint = quoteFingerprint(preview);
    const refreshed: PlanChangePreviewResult = {
      unchanged: false,
      classification: context.classification,
      scheduled: context.policy!.effective === "renewal",
      planId: context.targetOffer.plan,
      interval: context.targetInterval,
      preview,
      quoteFingerprint: fingerprint,
    };
    if (!input.quoteFingerprint || input.quoteFingerprint !== fingerprint) {
      throw new QuoteChangedError(refreshed);
    }
    if (!context.provider.changePlan) {
      throw new ProviderCapabilityNotSupportedError(context.provider.provider, "changePlan");
    }

    const existing = await this.billing.getOpenBillingSubscriptionChange(
      context.subscription.provider,
      context.subscription.providerSubscriptionId,
    );
    if (existing?.state === "scheduled" && existing.prorationBehavior === "none") {
      if (!context.provider.cancelScheduledPlanChange) {
        throw new ProviderCapabilityNotSupportedError(
          context.provider.provider,
          "cancelScheduledPlanChange",
        );
      }
      await context.provider.cancelScheduledPlanChange(
        context.subscription.providerSubscriptionId,
        existing.providerOperationId,
        `${input.operationKey}:replace`,
      );
      await this.billing.updateBillingSubscriptionChange(existing.id, {
        state: "canceled",
      });
    } else if (existing) {
      throw new CheckoutConflictError("A plan change is already awaiting payment");
    }

    const scheduled = context.policy!.effective === "renewal";
    const effectiveAt = scheduled
      ? preview.nextBillingDate
      : Number.isFinite(Date.parse(preview.effectiveAt))
        ? preview.effectiveAt
        : new Date().toISOString();
    if (!effectiveAt) {
      throw new CoreBillingDataUnavailableError(
        "The provider did not return the scheduled change date",
      );
    }
    const change = await this.billing.createBillingSubscriptionChange({
      provider: context.subscription.provider,
      providerSubscriptionId: context.subscription.providerSubscriptionId,
      toOfferId: context.targetOfferId,
      effectiveAt,
      idempotencyKey: input.operationKey,
      prorationBehavior: context.policy!.proration === "none" ? "none" : "invoice_immediately",
    });

    try {
      if (context.subscription.cancelAtPeriodEnd) {
        if (!context.provider.reactivateSubscription) {
          throw new ProviderCapabilityNotSupportedError(
            context.provider.provider,
            "reactivateSubscription",
          );
        }
        await context.provider.reactivateSubscription(
          context.subscription.providerSubscriptionId,
          `${input.operationKey}:keep`,
        );
      }
      const result = await context.provider.changePlan({
        providerSubscriptionId: context.subscription.providerSubscriptionId,
        productId: context.targetProductId,
        ...providerPlanChangeParams(context.policy!),
        onPaymentFailure: context.policy!.paymentFailure,
        metadata: {
          userId: input.accountId,
          plan_slug: context.targetOffer.plan,
          billing_interval: context.targetInterval,
        },
        idempotencyKey: input.operationKey,
      });
      await this.billing.updateBillingSubscriptionChange(change.id, {
        providerOperationId:
          result && "providerOperationId" in result ? (result.providerOperationId ?? null) : null,
      });
    } catch (error) {
      await this.billing.updateBillingSubscriptionChange(change.id, {
        state: "failed",
        errorMessage: error instanceof Error ? error.message : String(error),
      });
      throw error;
    }
    return {
      success: true,
      pending: true,
      scheduled,
      effectiveAt,
      planId: context.targetOffer.plan,
      interval: context.targetInterval,
    };
  }

  async cancelScheduledPlanChange(input: {
    accountId: string;
    operationKey: string;
  }): Promise<{ success: true }> {
    const subscription = await this.billing.getActiveSubscription(input.accountId);
    if (!subscription) throw new CommerceResourceNotFoundError("No active subscription found");
    const change = await this.billing.getOpenBillingSubscriptionChange(
      subscription.provider,
      subscription.providerSubscriptionId,
    );
    if (!change || change.state !== "scheduled" || change.prorationBehavior !== "none") {
      throw new CommerceResourceNotFoundError("No scheduled plan change found");
    }
    const provider = await this.providers.get(subscription.provider);
    if (!provider.cancelScheduledPlanChange) {
      throw new ProviderCapabilityNotSupportedError(provider.provider, "cancelScheduledPlanChange");
    }
    await provider.cancelScheduledPlanChange(
      subscription.providerSubscriptionId,
      change.providerOperationId,
      input.operationKey,
    );
    await this.billing.updateBillingSubscriptionChange(change.id, { state: "canceled" });
    return { success: true };
  }

  async createPortalSession(input: PortalSessionInput): Promise<{ url: string }> {
    const subscription = await this.billing.getUserSubscription(input.accountId);
    const customer = await this.billing.getCustomerByUserId(
      input.accountId,
      subscription?.provider,
    );
    if (!customer?.providerCustomerId) {
      throw new CommerceResourceNotFoundError("No billing customer found");
    }
    const provider = await this.providers.get(customer.provider);
    if (input.purpose === "payment-method") {
      if (subscription?.providerSubscriptionId) {
        if (!provider.createUpdatePaymentMethodSession) {
          throw new ProviderCapabilityNotSupportedError(
            provider.provider,
            "createUpdatePaymentMethodSession",
          );
        }
        return provider.createUpdatePaymentMethodSession({
          customerId: customer.providerCustomerId,
          subscriptionId: subscription.providerSubscriptionId,
          returnUrl: input.returnUrl,
        });
      }
      if (!provider.createPaymentMethodSetupSession) {
        throw new ProviderCapabilityNotSupportedError(
          provider.provider,
          "createPaymentMethodSetupSession",
        );
      }
      return provider.createPaymentMethodSetupSession({
        customerId: customer.providerCustomerId,
        returnUrl: input.returnUrl,
        cancelUrl: input.cancelUrl,
      });
    }
    if (!provider.createCustomerPortalSession) {
      throw new ProviderCapabilityNotSupportedError(
        provider.provider,
        "createCustomerPortalSession",
      );
    }
    return provider.createCustomerPortalSession({
      customerId: customer.providerCustomerId,
      returnUrl: input.returnUrl,
    });
  }

  private preferences(accountId: string, current: BillingPreferences | null): BillingPreferences {
    return {
      userId: accountId,
      ...DEFAULT_PREFERENCES,
      ...(this.options.preferenceDefaults ?? {}),
      ...(current ?? {}),
    };
  }

  async updatePreferences(input: {
    accountId: string;
    patch: PreferencePatch;
  }): Promise<BillingPreferences> {
    const current = await this.billing.getUserPreferences(input.accountId);
    const next = { ...this.preferences(input.accountId, current), ...input.patch };
    await this.billing.updateUserPreferences(next);
    return next;
  }

  private ledgerDocument(entry: LedgerEntry): BillingDocumentRef | null {
    const metadata = entry.metadata ?? {};
    const normalizedProvider =
      typeof metadata.provider === "string" ? metadata.provider : undefined;
    const normalizedDocumentId =
      typeof metadata.provider_document_id === "string"
        ? metadata.provider_document_id
        : typeof metadata.provider_invoice_id === "string"
          ? metadata.provider_invoice_id
          : typeof metadata.provider_payment_id === "string"
            ? metadata.provider_payment_id
            : undefined;
    const legacyDocumentId =
      typeof metadata.dodo_payment_id === "string" ? metadata.dodo_payment_id : undefined;
    if (!normalizedDocumentId && !legacyDocumentId) return null;
    return {
      kind: "ledger_entry",
      ledgerEntryId: entry.entryId,
      provider: normalizedProvider ?? (legacyDocumentId ? "dodo" : null),
      providerDocumentId: normalizedDocumentId ?? legacyDocumentId,
      createdAt: entry.createdAt,
      entryType: entry.entryType,
      amount: entry.amount,
    };
  }

  async getAccountOverview(accountId: string): Promise<AccountCommerceOverview> {
    let core;
    try {
      const [balance, available, bucketBalances, entitlement, allowance, subscription, prefs] =
        await Promise.all([
          this.credits.getBalance(accountId),
          this.credits.getAvailable(accountId),
          this.credits.getBucketBalances(accountId),
          this.credits.getUserPlan(accountId),
          this.credits.checkAllowance(accountId),
          this.billing.getUserSubscription(accountId),
          this.billing.getUserPreferences(accountId),
        ]);
      core = {
        balance,
        available,
        bucketBalances,
        entitlement,
        allowance,
        subscription,
        preferences: this.preferences(accountId, prefs),
      };
    } catch (error) {
      throw new CoreBillingDataUnavailableError(undefined, error);
    }

    const pendingChange = core.subscription
      ? await this.billing.getOpenBillingSubscriptionChange(
          core.subscription.provider,
          core.subscription.providerSubscriptionId,
        )
      : null;
    const [transactionsResult, usageResult, invoicesResult] = await Promise.allSettled([
      this.credits.listLedgerEntries(accountId, { limit: 50 }),
      this.credits.listUsageEntries(accountId, { limit: 100 }),
      this.billing.listBillingInvoices(accountId),
    ]);
    const transactions =
      transactionsResult.status === "fulfilled"
        ? transactionsResult.value.items.filter((entry) => entry.entryType !== "usage")
        : [];
    const usage = usageResult.status === "fulfilled" ? usageResult.value.items : [];
    const invoices = invoicesResult.status === "fulfilled" ? invoicesResult.value : [];
    const documents: BillingDocumentRef[] = [
      ...invoices.flatMap((invoice) => {
        const provider = invoice.provider ?? core.subscription?.provider;
        return provider
          ? [
              {
                kind: "provider_invoice" as const,
                provider,
                providerDocumentId: invoice.providerInvoiceId,
                status: invoice.status,
                amountPaidMinor: invoice.amountPaidMinor,
                amountDueMinor: invoice.amountDueMinor,
                currency: invoice.currency,
                periodStart: invoice.periodStart,
                periodEnd: invoice.periodEnd,
              },
            ]
          : [];
      }),
      ...transactions.flatMap((entry) => {
        const document = this.ledgerDocument(entry);
        return document ? [document] : [];
      }),
    ];

    let paymentMethods: PaymentMethodInfo[] = [];
    let paymentMethodsAvailable = true;
    let autoRecharge = null;
    let autoRechargeAvailable = true;
    try {
      const customer = await this.billing.getCustomerByUserId(
        accountId,
        core.subscription?.provider,
      );
      if (customer) {
        const provider = await this.providers.get(customer.provider);
        if (provider.listPaymentMethods) {
          paymentMethods = await provider.listPaymentMethods(customer.providerCustomerId);
        } else {
          paymentMethodsAvailable = false;
        }
        try {
          autoRecharge = await this.billing.autoRecharge.getStatus({
            userId: accountId,
            provider,
          });
        } catch (error) {
          autoRechargeAvailable = false;
          this.logger.warn("[CommerceService] auto-recharge overview unavailable", {
            accountId,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      }
    } catch (error) {
      paymentMethodsAvailable = false;
      autoRechargeAvailable = false;
      this.logger.warn("[CommerceService] provider overview unavailable", {
        accountId,
        error: error instanceof Error ? error.message : String(error),
      });
    }

    return {
      accountId,
      credits: {
        ledgerBalance: core.balance.balance,
        effectiveSpendableBalance: core.available.available,
        lifetimePurchases: core.balance.lifetimePurchased,
        allowance: {
          remaining: core.allowance.allowanceRemaining,
          limit: core.entitlement.allowanceAmount,
          periodStart: core.allowance.periodStart ?? null,
          periodEnd: core.allowance.periodEnd ?? null,
        },
        buckets: core.bucketBalances.buckets,
      },
      entitlement: core.entitlement,
      subscription: core.subscription,
      pendingChange,
      preferences: core.preferences,
      paymentMethods,
      documents,
      providerInvoices: invoices,
      transactions,
      usage,
      autoRecharge,
      availability: {
        paymentMethods: paymentMethodsAvailable,
        documents:
          invoicesResult.status === "fulfilled" && transactionsResult.status === "fulfilled",
        transactions: transactionsResult.status === "fulfilled",
        usage: usageResult.status === "fulfilled",
        autoRecharge: autoRechargeAvailable,
      },
    };
  }

  async getInvoiceLink(input: GetInvoiceLinkInput): Promise<{ url: string }> {
    let providerName: string;
    let providerDocumentId: string;
    if (input.document.kind === "provider_invoice") {
      const document = input.document;
      const owned = (await this.billing.listBillingInvoices(input.accountId)).find(
        (invoice) =>
          (invoice.provider ?? document.provider) === document.provider &&
          invoice.providerInvoiceId === document.providerDocumentId,
      );
      if (!owned) throw new CommerceResourceNotFoundError("Invoice not found");
      providerName = document.provider;
      providerDocumentId = owned.providerInvoiceId;
    } else {
      const entry = await this.credits.getLedgerEntry(
        input.accountId,
        input.document.ledgerEntryId,
      );
      if (!entry) throw new CommerceResourceNotFoundError("Ledger entry not found");
      const owned = this.ledgerDocument(entry);
      if (!owned?.provider || !owned.providerDocumentId) {
        throw new CommerceResourceNotFoundError(
          "No provider document is associated with the ledger entry",
        );
      }
      providerName = owned.provider;
      providerDocumentId = owned.providerDocumentId;
    }
    const provider = await this.providers.get(providerName);
    if (!provider.getInvoiceUrl) {
      throw new ProviderCapabilityNotSupportedError(provider.provider, "getInvoiceUrl");
    }
    const result = await provider.getInvoiceUrl(providerDocumentId);
    if (!result) throw new CommerceResourceNotFoundError("No invoice URL is available");
    return result;
  }

  async handleWebhook(input: CommerceWebhookInput): Promise<CommerceWebhookResult> {
    const provider = await this.providers.select({ requested: input.provider });
    const result = await provider.handleWebhook({
      rawBody: input.rawBody,
      headers: input.headers,
    });
    return {
      received: result.received,
      retryable: result.retryable,
      provider: result.provider,
      eventId: result.eventId,
      eventType: result.eventType,
    };
  }
}
