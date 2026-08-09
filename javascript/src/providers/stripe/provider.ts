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
import { ProviderResponseError } from "../../errors.js";
import { requireStableKey, scopedStableKey } from "../../shared/idempotency.js";
import { handleStripeWebhook } from "./event-mapper.js";

function requireStripeText(value: unknown, operation: string, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ProviderResponseError("stripe", operation, { details: { field } });
  }
  return value;
}

function requireStripeInteger(
  value: unknown,
  operation: string,
  field: string,
  minimum = 0,
): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    throw new ProviderResponseError("stripe", operation, { details: { field } });
  }
  return value;
}

function requireStripeNumber(value: unknown, operation: string, field: string): number {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value)
        ? Number(value)
        : Number.NaN;
  if (!Number.isFinite(parsed)) {
    throw new ProviderResponseError("stripe", operation, { details: { field } });
  }
  return parsed;
}

function savedPaymentStatus(
  status: Stripe.PaymentIntent.Status,
): SavedPaymentChargeResult["status"] {
  switch (status) {
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
      throw new ProviderResponseError("stripe", "chargeSavedPaymentMethod", {
        details: { field: "status" },
      });
  }
}

function requestOptions(idempotencyKey: string): Stripe.RequestOptions {
  return { idempotencyKey };
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
  const items = phase.items.map((item) => ({
    price: requireStripeText(expandableId(item.price), "changePlan", "schedule.phases.items.price"),
    quantity: item.quantity,
    ...(item.metadata ? { metadata: item.metadata } : {}),
    ...(item.tax_rates
      ? {
          tax_rates: item.tax_rates.map((taxRate) =>
            requireStripeText(
              expandableId(taxRate),
              "changePlan",
              "schedule.phases.items.tax_rates",
            ),
          ),
        }
      : {}),
  }));
  if (items.length === 0) {
    throw new ProviderResponseError("stripe", "changePlan", {
      details: { field: "schedule.phases.items" },
    });
  }
  return {
    items,
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
    const idempotencyKey = requireStableKey(params.idempotencyKey);
    this.logger.info("[StripeProvider] createCheckoutSession", {
      productId: params.productId,
      type: params.type,
      hasUserId: Boolean(params.userId),
    });
    if (!params.userId) throw new TypeError("Authentication required for checkout");
    const stripe = this.getStripe();

    let customerId = params.customerId;
    if (!customerId) {
      const customer = await stripe.customers.create(
        {
          ...(params.email ? { email: params.email } : {}),
          metadata: { userId: params.userId },
        },
        requestOptions(scopedStableKey(idempotencyKey, "customer")),
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
      requestOptions(idempotencyKey),
    );

    return {
      url: requireStripeText(session.url, "createCheckoutSession", "url"),
      customerId: requireStripeText(customerId, "createCheckoutSession", "customer"),
      providerSessionId: requireStripeText(session.id, "createCheckoutSession", "id"),
    };
  }

  async getCheckoutSessionStatus(providerSessionId: string): Promise<{
    paymentStatus: CheckoutPaymentStatus;
  } | null> {
    let session: Stripe.Checkout.Session;
    try {
      session = await this.getStripe().checkout.sessions.retrieve(providerSessionId, {
        expand: ["payment_intent"],
      });
    } catch (error) {
      if (
        error instanceof Stripe.errors.StripeInvalidRequestError &&
        error.code === "resource_missing"
      ) {
        return null;
      }
      throw error;
    }
    if (session.status === "expired") return { paymentStatus: "cancelled" };
    if (session.payment_status === "paid" || session.payment_status === "no_payment_required") {
      return { paymentStatus: "succeeded" };
    }
    if (session.status === "open") return { paymentStatus: "processing" };
    const intent =
      session.payment_intent && typeof session.payment_intent !== "string"
        ? session.payment_intent
        : null;
    return { paymentStatus: mapPaymentIntentStatus(intent) };
  }

  async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
    const stripe = this.getStripe();
    const session = await stripe.billingPortal.sessions.create({
      customer: params.customerId,
      return_url: params.returnUrl,
    });
    return { url: requireStripeText(session.url, "createCustomerPortalSession", "url") };
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
    return { url: requireStripeText(session.url, "createUpdatePaymentMethodSession", "url") };
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
    return { url: requireStripeText(session.url, "createPaymentMethodSetupSession", "url") };
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

  async cancelSubscription(subscriptionId: string, idempotencyKey: string): Promise<void> {
    const stableKey = requireStableKey(idempotencyKey);
    const stripe = this.getStripe();
    await stripe.subscriptions.update(
      subscriptionId,
      { cancel_at_period_end: true },
      { idempotencyKey: stableKey },
    );
  }

  async reactivateSubscription(subscriptionId: string, idempotencyKey: string): Promise<void> {
    const stableKey = requireStableKey(idempotencyKey);
    const stripe = this.getStripe();
    await stripe.subscriptions.update(
      subscriptionId,
      { cancel_at_period_end: false },
      { idempotencyKey: stableKey },
    );
  }

  async cancelScheduledPlanChange(
    _subscriptionId: string,
    providerOperationId: string | null | undefined,
    idempotencyKey: string,
  ): Promise<void> {
    const stableKey = requireStableKey(idempotencyKey);
    if (!providerOperationId) throw new TypeError("Stripe scheduled change has no schedule ID");
    await this.getStripe().subscriptionSchedules.release(
      providerOperationId,
      {},
      { idempotencyKey: stableKey },
    );
  }

  async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
    const stripe = this.getStripe();
    const [customer, methods] = await Promise.all([
      stripe.customers.retrieve(customerId),
      stripe.customers.listPaymentMethods(customerId, { type: "card" }),
    ]);
    if (customer.deleted) return [];
    const defaultPaymentMethod = customer.invoice_settings.default_payment_method;
    const defaultId =
      typeof defaultPaymentMethod === "string"
        ? defaultPaymentMethod
        : (defaultPaymentMethod?.id ?? null);
    return deduplicatePaymentMethods(
      methods.data.map((method) => {
        const card = method.card;
        if (card === null || card === undefined) {
          throw new ProviderResponseError("stripe", "listPaymentMethods", {
            details: { field: "card" },
          });
        }
        const last4 = requireStripeText(card.last4, "listPaymentMethods", "card.last4");
        if (!/^\d{4}$/.test(last4)) {
          throw new ProviderResponseError("stripe", "listPaymentMethods", {
            details: { field: "card.last4" },
          });
        }
        const id = requireStripeText(method.id, "listPaymentMethods", "id");
        return {
          id,
          last4,
          brand: requireStripeText(card.brand, "listPaymentMethods", "card.brand"),
          expiryMonth: requireStripeInteger(
            card.exp_month,
            "listPaymentMethods",
            "card.exp_month",
            1,
          ),
          expiryYear: requireStripeInteger(card.exp_year, "listPaymentMethods", "card.exp_year", 1),
          isDefault: id === defaultId,
        };
      }),
    );
  }

  async previewSavedPaymentCharge(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeQuote> {
    const price = await this.getStripe().prices.retrieve(params.productId);
    if (price.unit_amount == null) {
      throw new ProviderResponseError("stripe", "previewSavedPaymentCharge", {
        details: { field: "unit_amount" },
      });
    }
    const amountMinor = price.unit_amount * params.quantity;
    return {
      amountMinor: requireStripeInteger(amountMinor, "previewSavedPaymentCharge", "amount"),
      currency: requireStripeText(price.currency, "previewSavedPaymentCharge", "currency"),
    };
  }

  async chargeSavedPaymentMethod(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeResult> {
    const stripe = this.getStripe();
    const price = await stripe.prices.retrieve(params.productId);
    if (price.unit_amount == null) {
      throw new ProviderResponseError("stripe", "chargeSavedPaymentMethod", {
        details: { field: "unit_amount" },
      });
    }
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
    return {
      providerPaymentId: requireStripeText(intent.id, "chargeSavedPaymentMethod", "id"),
      status: savedPaymentStatus(intent.status),
      amountMinor: requireStripeInteger(intent.amount, "chargeSavedPaymentMethod", "amount"),
      currency: requireStripeText(intent.currency, "chargeSavedPaymentMethod", "currency"),
    };
  }

  async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
    const stripe = this.getStripe();
    const idempotencyKey = requireStableKey(params.idempotencyKey);
    const customer = await stripe.customers.create(
      {
        email: params.email,
        name: params.name,
        metadata: params.metadata,
      },
      requestOptions(idempotencyKey),
    );
    return { customerId: requireStripeText(customer.id, "createCustomer", "id") };
  }

  async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
    const stripe = this.getStripe();
    const invoice = await stripe.invoices.retrieve(providerPaymentId);
    return invoice.hosted_invoice_url === null
      ? null
      : {
          url: requireStripeText(invoice.hosted_invoice_url, "getInvoiceUrl", "hosted_invoice_url"),
        };
  }

  async changePlan(params: ChangePlanParams): Promise<{ providerOperationId?: string }> {
    const idempotencyKey = requireStableKey(params.idempotencyKey);
    const stripe = this.getStripe();
    const subscription = await stripe.subscriptions.retrieve(params.providerSubscriptionId);
    const item = subscription.items.data[0];
    if (!item) {
      throw new ProviderResponseError("stripe", "changePlan", {
        details: { field: "subscription.items" },
      });
    }
    if (params.effectiveAt === "next_billing_date") {
      const schedule = await stripe.subscriptionSchedules.create(
        { from_subscription: params.providerSubscriptionId },
        requestOptions(scopedStableKey(idempotencyKey, "schedule-create")),
      );
      const currentPhase = schedule.phases[0];
      if (!currentPhase) {
        throw new ProviderResponseError("stripe", "changePlan", {
          details: { field: "schedule.phases" },
        });
      }
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
        requestOptions(scopedStableKey(idempotencyKey, "schedule-update")),
      );
      return {
        providerOperationId: requireStripeText(schedule.id, "changePlan", "schedule.id"),
      };
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
      requestOptions(scopedStableKey(idempotencyKey, "subscription-update")),
    );
    const latestInvoiceId = expandableId(updated.latest_invoice);
    return latestInvoiceId === undefined ? {} : { providerOperationId: latestInvoiceId };
  }

  async previewChangePlan(params: PreviewChangePlanParams): Promise<ChangePlanPreview> {
    const stripe = this.getStripe();
    const subscription = await stripe.subscriptions.retrieve(params.providerSubscriptionId);
    const item = subscription.items.data[0];
    if (!item) {
      throw new ProviderResponseError("stripe", "previewChangePlan", {
        details: { field: "subscription.items" },
      });
    }
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
    const currentPeriodEnd = requireStripeInteger(
      item.current_period_end,
      "previewChangePlan",
      "subscription.items.current_period_end",
      1,
    );
    const lineItems = invoice.lines.data
      .filter((line) => line.parent !== null && line.parent.subscription_item_details !== null)
      .map((line) => {
        const operation = "previewChangePlan";
        const priceDetails = line.pricing?.price_details;
        if (!priceDetails) {
          throw new ProviderResponseError("stripe", operation, {
            details: { field: "invoice.lines.pricing.price_details" },
          });
        }
        const quantity =
          line.quantity === null
            ? 1
            : requireStripeInteger(line.quantity, operation, "invoice.lines.quantity", 1);
        const unitPrice = requireStripeNumber(
          line.pricing?.unit_amount_decimal,
          operation,
          "invoice.lines.pricing.unit_amount_decimal",
        );
        const subtotal = requireStripeInteger(
          line.subtotal,
          operation,
          "invoice.lines.subtotal",
          Number.MIN_SAFE_INTEGER,
        );
        const expectedSubtotal = unitPrice * quantity;
        const tax =
          line.taxes?.reduce(
            (total, taxItem) =>
              total + requireStripeInteger(taxItem.amount, operation, "invoice.lines.taxes.amount"),
            0,
          ) ?? 0;
        return {
          productId: requireStripeText(
            expandableId(priceDetails.price),
            operation,
            "invoice.lines.pricing.price_details.price",
          ),
          name: requireStripeText(line.description, operation, "invoice.lines.description"),
          unitPrice,
          quantity,
          prorationFactor: expectedSubtotal === 0 ? 1 : subtotal / expectedSubtotal,
          currency: requireStripeText(line.currency, operation, "invoice.lines.currency"),
          tax,
          subtotal,
        };
      });
    return {
      totalAmount: requireStripeInteger(invoice.total, "previewChangePlan", "invoice.total"),
      settlementAmount: requireStripeInteger(
        invoice.amount_due,
        "previewChangePlan",
        "invoice.amount_due",
      ),
      currency: requireStripeText(invoice.currency, "previewChangePlan", "invoice.currency"),
      lineItems,
      effectiveAt:
        params.effectiveAt === "next_billing_date"
          ? new Date(currentPeriodEnd * 1000).toISOString()
          : new Date(invoice.created * 1000).toISOString(),
      ...(price.unit_amount === null ? {} : { recurringAmount: price.unit_amount }),
      recurringCurrency: requireStripeText(price.currency, "previewChangePlan", "price.currency"),
      nextBillingDate: new Date(currentPeriodEnd * 1000).toISOString(),
      taxAmount:
        invoice.total_taxes?.reduce(
          (total, taxItem) =>
            total +
            requireStripeInteger(taxItem.amount, "previewChangePlan", "invoice.total_taxes.amount"),
          0,
        ) ?? 0,
    };
  }
}
