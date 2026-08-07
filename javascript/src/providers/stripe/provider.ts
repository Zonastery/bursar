import Stripe from "stripe";
import type { CheckoutPaymentStatus, PaymentProvider } from "../types.js";
import {
  deduplicatePaymentMethods,
  type ProviderLogger,
  normalizeProviderLogger,
} from "../types.js";
import type {
  CheckoutParams,
  CheckoutSessionResult,
  PortalParams,
  UpdatePaymentMethodParams,
  PaymentMethodSetupParams,
  CreateCustomerParams,
  PaymentMethodInfo,
  WebhookRequest,
  ChangePlanParams,
  PreviewChangePlanParams,
  ChangePlanPreview,
  SavedPaymentChargeParams,
  SavedPaymentChargeResult,
  SavedPaymentChargeQuote,
  WebhookResult,
} from "../types.js";
import type { BillingEventSink } from "../../bursar.js";
import { handleStripeWebhook } from "./event-mapper.js";

function scopedIdempotencyKey(key: string | undefined, scope: string): string | undefined {
  if (!key) return undefined;
  const suffix = `:${scope}`;
  return `${key.slice(0, 255 - suffix.length)}${suffix}`;
}

function requestOptions(idempotencyKey: string | undefined): Stripe.RequestOptions | undefined {
  return idempotencyKey ? { idempotencyKey } : undefined;
}

function expandableId(value: string | { id: string } | null | undefined): string | undefined {
  if (!value) return undefined;
  return typeof value === "string" ? value : value.id;
}

function mapPaymentIntentStatus(intent: Stripe.PaymentIntent | null): CheckoutPaymentStatus {
  switch (intent?.status) {
    case "succeeded":
      return "succeeded";
    case "processing":
      return "processing";
    case "requires_action":
      return "requires_customer_action";
    case "requires_payment_method":
      return "requires_payment_method";
    case "requires_confirmation":
      return "requires_confirmation";
    case "requires_capture":
      return "requires_capture";
    case "canceled":
      return "cancelled";
    default:
      return null;
  }
}

function schedulePhaseParams(
  phase: Stripe.SubscriptionSchedule.Phase,
): Stripe.SubscriptionScheduleUpdateParams.Phase {
  return {
    items: phase.items.map((item) => ({
      price: expandableId(item.price),
      quantity: item.quantity,
      ...(item.metadata ? { metadata: item.metadata } : {}),
      ...(item.tax_rates
        ? { tax_rates: item.tax_rates.map((taxRate) => expandableId(taxRate)!) }
        : {}),
    })),
    start_date: phase.start_date,
    end_date: phase.end_date,
    ...(phase.automatic_tax ? { automatic_tax: { enabled: phase.automatic_tax.enabled } } : {}),
    ...(phase.billing_cycle_anchor ? { billing_cycle_anchor: phase.billing_cycle_anchor } : {}),
    ...(phase.collection_method ? { collection_method: phase.collection_method } : {}),
    ...(phase.currency ? { currency: phase.currency } : {}),
    ...(expandableId(phase.default_payment_method)
      ? { default_payment_method: expandableId(phase.default_payment_method) }
      : {}),
    ...(phase.description != null ? { description: phase.description } : {}),
    ...(phase.metadata ? { metadata: phase.metadata } : {}),
    ...(phase.proration_behavior ? { proration_behavior: phase.proration_behavior } : {}),
    ...(phase.trial_end != null ? { trial_end: phase.trial_end } : {}),
  };
}

function stripeProrationBehavior(
  mode: ChangePlanParams["prorationBillingMode"],
): Stripe.SubscriptionUpdateParams.ProrationBehavior {
  return mode === "do_not_bill" ? "none" : "always_invoice";
}

export interface StripeProviderOptions {
  getClient: () => Stripe;
  webhookSecret: string;
  eventSink: BillingEventSink;
  logger?: ProviderLogger | null;
}

export class StripeProvider implements PaymentProvider {
  readonly provider = "stripe" as const;
  private readonly getStripe: () => Stripe;
  private readonly sink: BillingEventSink;
  private readonly webhookSecret: string;
  private readonly logger: ReturnType<typeof normalizeProviderLogger>;

  constructor(options: StripeProviderOptions) {
    if (!options.webhookSecret.trim()) throw new TypeError("webhookSecret must not be empty");
    this.getStripe = options.getClient;
    this.sink = options.eventSink;
    this.webhookSecret = options.webhookSecret;
    this.logger = normalizeProviderLogger(options.logger);
  }

  async createCheckoutSession(params: CheckoutParams): Promise<CheckoutSessionResult> {
    this.logger.info("[StripeProvider] createCheckoutSession", {
      productId: params.productId,
      type: params.type,
      hasUserId: Boolean(params.userId),
    });
    if (!params.userId) throw new Error("Authentication required for checkout");
    const stripe = this.getStripe();

    let customerId = params.customerId;
    if (!customerId) {
      const customer = await stripe.customers.create(
        {
          ...(params.email ? { email: params.email } : {}),
          metadata: { userId: params.userId },
        },
        requestOptions(scopedIdempotencyKey(params.idempotencyKey, "customer")),
      );
      customerId = customer.id;
    }

    const sessionOpts: Stripe.Checkout.SessionCreateParams = {
      customer: customerId,
      mode: params.type === "subscription" ? "subscription" : "payment",
      line_items: [{ price: params.productId, quantity: params.quantity ?? 1 }],
      success_url: params.returnUrl,
      cancel_url: params.cancelUrl,
      client_reference_id: params.userId,
      automatic_tax: { enabled: true },
      metadata: params.metadata,
      ...(params.type === "subscription"
        ? { subscription_data: { metadata: { userId: params.userId, ...params.metadata } } }
        : { payment_intent_data: { metadata: { userId: params.userId, ...params.metadata } } }),
    };
    const session = await stripe.checkout.sessions.create(
      sessionOpts,
      requestOptions(params.idempotencyKey),
    );

    if (!session.url) throw new Error("Stripe checkout session returned no URL");
    return { url: session.url, customerId, providerSessionId: session.id };
  }

  async getCheckoutSessionStatus(providerSessionId: string): Promise<{
    paymentStatus: CheckoutPaymentStatus;
  } | null> {
    const session = await this.getStripe().checkout.sessions.retrieve(providerSessionId, {
      expand: ["payment_intent"],
    });
    if (session.status === "expired") return { paymentStatus: "cancelled" };
    if (session.payment_status === "paid" || session.payment_status === "no_payment_required") {
      return { paymentStatus: "succeeded" };
    }
    if (session.status === "open") return { paymentStatus: "processing" };
    const intent =
      session.payment_intent && typeof session.payment_intent !== "string"
        ? session.payment_intent
        : null;
    return { paymentStatus: mapPaymentIntentStatus(intent) ?? "processing" };
  }

  async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
    const stripe = this.getStripe();
    const session = await stripe.billingPortal.sessions.create({
      customer: params.customerId,
      return_url: params.returnUrl,
    });
    if (!session.url) throw new Error("Stripe portal session returned no URL");
    return { url: session.url };
  }

  async createUpdatePaymentMethodSession(
    params: UpdatePaymentMethodParams,
  ): Promise<{ url: string }> {
    const stripe = this.getStripe();
    const session = await stripe.billingPortal.sessions.create({
      customer: params.customerId,
      return_url: params.returnUrl,
      flow_data: { type: "payment_method_update" },
    });
    if (!session.url) throw new Error("Stripe portal session returned no URL");
    return { url: session.url };
  }

  async createPaymentMethodSetupSession(
    params: PaymentMethodSetupParams,
  ): Promise<{ url: string }> {
    const stripe = this.getStripe();
    const session = await stripe.checkout.sessions.create({
      customer: params.customerId,
      mode: "setup",
      success_url: params.returnUrl,
      cancel_url: params.cancelUrl ?? params.returnUrl,
      payment_method_types: ["card"],
    });
    if (!session.url) throw new Error("Stripe setup session returned no URL");
    return { url: session.url };
  }

  async handleWebhook(req: WebhookRequest): Promise<WebhookResult> {
    const stripe = this.getStripe();
    const signature = req.headers["stripe-signature"];
    if (!signature) {
      return {
        received: false,
        retryable: false,
        provider: this.provider,
        eventId: null,
        eventType: null,
      };
    }

    let event: Stripe.Event;
    try {
      event = stripe.webhooks.constructEvent(req.rawBody, signature, this.webhookSecret);
    } catch (err) {
      if (err instanceof Stripe.errors.StripeSignatureVerificationError) {
        return {
          received: false,
          retryable: false,
          provider: this.provider,
          eventId: null,
          eventType: null,
        };
      }
      throw err;
    }

    const result = await handleStripeWebhook(event, this.sink, stripe, this.logger);
    return {
      received: result.received,
      retryable: false,
      provider: this.provider,
      eventId: event.id,
      eventType: event.type,
    };
  }

  async cancelSubscription(subscriptionId: string, idempotencyKey?: string): Promise<void> {
    const stripe = this.getStripe();
    await stripe.subscriptions.update(
      subscriptionId,
      { cancel_at_period_end: true },
      idempotencyKey ? { idempotencyKey } : undefined,
    );
  }

  async reactivateSubscription(subscriptionId: string, idempotencyKey?: string): Promise<void> {
    const stripe = this.getStripe();
    await stripe.subscriptions.update(
      subscriptionId,
      { cancel_at_period_end: false },
      idempotencyKey ? { idempotencyKey } : undefined,
    );
  }

  async cancelScheduledPlanChange(
    _subscriptionId: string,
    providerOperationId?: string | null,
    idempotencyKey?: string,
  ): Promise<void> {
    if (!providerOperationId) throw new Error("Stripe scheduled change has no schedule ID");
    await this.getStripe().subscriptionSchedules.release(
      providerOperationId,
      {},
      idempotencyKey ? { idempotencyKey } : undefined,
    );
  }

  async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
    const stripe = this.getStripe();
    const [customer, methods] = await Promise.all([
      stripe.customers.retrieve(customerId),
      stripe.paymentMethods.list({ customer: customerId, type: "card" }),
    ]);
    if (customer.deleted) return [];
    const defaultPaymentMethod = customer.invoice_settings.default_payment_method;
    const defaultId =
      typeof defaultPaymentMethod === "string"
        ? defaultPaymentMethod
        : (defaultPaymentMethod?.id ?? null);
    return deduplicatePaymentMethods(
      methods.data.map((pm) => ({
        id: pm.id,
        last4: pm.card?.last4 ?? "",
        brand: pm.card?.brand ?? "unknown",
        expiryMonth: pm.card?.exp_month ?? 0,
        expiryYear: pm.card?.exp_year ?? 0,
        isDefault: pm.id === defaultId,
      })),
    );
  }

  async previewSavedPaymentCharge(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeQuote> {
    const price = await this.getStripe().prices.retrieve(params.productId);
    if (price.unit_amount == null) throw new Error("Stripe top-up price has no fixed amount");
    return { amountMinor: price.unit_amount * params.quantity, currency: price.currency };
  }

  async chargeSavedPaymentMethod(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeResult> {
    const stripe = this.getStripe();
    const price = await stripe.prices.retrieve(params.productId);
    if (price.unit_amount == null) throw new Error("Stripe top-up price has no fixed amount");
    const intent = await stripe.paymentIntents.create(
      {
        amount: price.unit_amount * params.quantity,
        currency: price.currency,
        customer: params.customerId,
        payment_method: params.paymentMethodId,
        confirm: true,
        off_session: true,
        metadata: { ...params.metadata, price_id: params.productId },
      },
      { idempotencyKey: params.idempotencyKey },
    );
    const status: SavedPaymentChargeResult["status"] =
      intent.status === "succeeded"
        ? "succeeded"
        : intent.status === "processing"
          ? "processing"
          : intent.status === "requires_action"
            ? "requires_customer_action"
            : intent.status === "requires_payment_method"
              ? "requires_payment_method"
              : "failed";
    return {
      providerPaymentId: intent.id,
      status,
      amountMinor: intent.amount,
      currency: intent.currency,
    };
  }

  async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
    const stripe = this.getStripe();
    const customer = await stripe.customers.create({
      email: params.email,
      name: params.name,
      metadata: params.metadata,
    });
    return { customerId: customer.id };
  }

  async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
    const stripe = this.getStripe();
    const invoice = await stripe.invoices.retrieve(providerPaymentId);
    return invoice.hosted_invoice_url ? { url: invoice.hosted_invoice_url } : null;
  }

  async changePlan(params: ChangePlanParams): Promise<{ providerOperationId?: string }> {
    const stripe = this.getStripe();
    const subscription = await stripe.subscriptions.retrieve(params.providerSubscriptionId);
    const item = subscription.items.data[0];
    if (!item) throw new Error("Stripe subscription has no billing item");
    if (params.effectiveAt === "next_billing_date") {
      const schedule = await stripe.subscriptionSchedules.create(
        { from_subscription: params.providerSubscriptionId },
        requestOptions(scopedIdempotencyKey(params.idempotencyKey, "schedule-create")),
      );
      const currentPhase = schedule.phases[0];
      if (!currentPhase) throw new Error("Stripe subscription schedule has no current phase");
      await stripe.subscriptionSchedules.update(
        schedule.id,
        {
          phases: [
            schedulePhaseParams(currentPhase),
            {
              items: [{ price: params.productId, quantity: params.quantity ?? 1 }],
              start_date: currentPhase.end_date,
              proration_behavior: "none",
              ...(params.metadata ? { metadata: params.metadata } : {}),
            },
          ],
          proration_behavior: "none",
        },
        requestOptions(scopedIdempotencyKey(params.idempotencyKey, "schedule-update")),
      );
      return { providerOperationId: schedule.id };
    }
    const updated = await stripe.subscriptions.update(
      params.providerSubscriptionId,
      {
        items: [{ id: item.id, price: params.productId, quantity: params.quantity ?? 1 }],
        proration_behavior: stripeProrationBehavior(params.prorationBillingMode),
        payment_behavior:
          params.onPaymentFailure === "apply_change" ? "allow_incomplete" : "pending_if_incomplete",
        ...(params.metadata ? { metadata: params.metadata } : {}),
      },
      requestOptions(scopedIdempotencyKey(params.idempotencyKey, "subscription-update")),
    );
    return {
      providerOperationId: updated.latest_invoice ? String(updated.latest_invoice) : undefined,
    };
  }

  async previewChangePlan(params: PreviewChangePlanParams): Promise<ChangePlanPreview> {
    const stripe = this.getStripe();
    const subscription = await stripe.subscriptions.retrieve(params.providerSubscriptionId);
    const item = subscription.items.data[0];
    if (!item) throw new Error("Stripe subscription has no billing item");
    const invoice = await stripe.invoices.createPreview({
      customer: String(subscription.customer),
      subscription: params.providerSubscriptionId,
      subscription_details: {
        items: [{ id: item.id, price: params.productId, quantity: params.quantity ?? 1 }],
        proration_behavior:
          params.effectiveAt === "next_billing_date"
            ? "none"
            : stripeProrationBehavior(params.prorationBillingMode),
      },
    });
    const price = await stripe.prices.retrieve(params.productId);
    return {
      totalAmount: invoice.total ?? 0,
      settlementAmount: invoice.amount_due ?? 0,
      currency: invoice.currency,
      lineItems: invoice.lines.data.map((line) => {
        const tax = line.taxes?.reduce((total, item) => total + item.amount, 0) ?? 0;
        return {
          productId: params.productId,
          name: line.description ?? "Subscription change",
          unitPrice: line.amount ?? 0,
          quantity: line.quantity ?? 1,
          prorationFactor: 1,
          currency: line.currency ?? invoice.currency,
          tax,
          subtotal: line.amount ?? 0,
        };
      }),
      effectiveAt:
        params.effectiveAt === "next_billing_date"
          ? new Date(item.current_period_end * 1000).toISOString()
          : new Date().toISOString(),
      recurringAmount: price.unit_amount ?? 0,
      recurringCurrency: price.currency,
      nextBillingDate: new Date(item.current_period_end * 1000).toISOString(),
      taxAmount: invoice.total_taxes?.reduce((total, item) => total + item.amount, 0) ?? 0,
    };
  }
}
