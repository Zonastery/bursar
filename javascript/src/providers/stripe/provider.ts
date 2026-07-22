import Stripe from "stripe";
import type { CheckoutPaymentStatus, PaymentProvider } from "../types.js";
import {
  deduplicatePaymentMethods,
  type ProviderLogger,
  normalizeProviderLogger,
} from "../types.js";
import type {
  CheckoutParams,
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
} from "../types.js";
import type { BillingEventSink } from "../../bursar.js";
import { handleStripeWebhook } from "./event-mapper.js";

export class StripeProvider implements PaymentProvider {
  readonly provider = "stripe" as const;

  constructor(
    private getStripe: () => Stripe,
    private sink: BillingEventSink,
    private webhookSecret: string,
    logger?: ProviderLogger | null,
  ) {
    this.logger = normalizeProviderLogger(logger);
  }

  private logger: ReturnType<typeof normalizeProviderLogger>;

  async createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string }> {
    this.logger.info("[StripeProvider] createCheckoutSession", {
      productId: params.productId,
      type: params.type,
      hasUserId: Boolean(params.userId),
    });
    if (!params.userId) throw new Error("Authentication required for checkout");
    const stripe = this.getStripe();

    let customerId = params.customerId;
    if (!customerId) {
      const customer = await stripe.customers.create({
        metadata: { userId: params.userId },
      });
      customerId = customer.id;
    }

    const sessionOpts: Stripe.Checkout.SessionCreateParams & {
      idempotencyKey?: string;
    } = {
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
    if (params.idempotencyKey) {
      sessionOpts.idempotencyKey = params.idempotencyKey;
    }
    const session = await stripe.checkout.sessions.create(sessionOpts);

    if (!session.url) throw new Error("Stripe checkout session returned no URL");
    return { url: session.url, customerId };
  }

  async getCheckoutSessionStatus(providerSessionId: string): Promise<{
    paymentStatus: CheckoutPaymentStatus;
  } | null> {
    const session = await this.getStripe().checkout.sessions.retrieve(providerSessionId);
    if (session.status === "expired") return { paymentStatus: "cancelled" };
    if (session.payment_status === "paid" || session.payment_status === "no_payment_required") {
      return { paymentStatus: "succeeded" };
    }
    if (session.payment_status === "unpaid") return { paymentStatus: "requires_payment_method" };
    return { paymentStatus: null };
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

  async handleWebhook(req: WebhookRequest): Promise<{ received: boolean; retryable?: boolean }> {
    const stripe = this.getStripe();
    const signature = req.headers["stripe-signature"];
    if (!signature) return { received: false, retryable: false };

    let event: Stripe.Event;
    try {
      event = stripe.webhooks.constructEvent(req.rawBody, signature, this.webhookSecret);
    } catch (err) {
      if (err instanceof Stripe.errors.StripeSignatureVerificationError) {
        return { received: false, retryable: false };
      }
      throw err;
    }

    return handleStripeWebhook(event, this.sink, stripe, this.logger);
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
    const methods = await stripe.paymentMethods.list({
      customer: customerId,
      type: "card",
    });
    return deduplicatePaymentMethods(
      methods.data.map((pm) => ({
        id: pm.id,
        last4: pm.card?.last4 ?? "",
        brand: pm.card?.brand ?? "unknown",
        expiryMonth: pm.card?.exp_month ?? 0,
        expiryYear: pm.card?.exp_year ?? 0,
      })),
    );
  }

  async getDefaultPaymentMethod(customerId: string): Promise<PaymentMethodInfo | null> {
    const customer = await this.getStripe().customers.retrieve(customerId);
    if (customer.deleted) return null;
    const defaultId =
      typeof customer.invoice_settings.default_payment_method === "string"
        ? customer.invoice_settings.default_payment_method
        : customer.invoice_settings.default_payment_method?.id;
    if (!defaultId) return null;
    return (
      (await this.listPaymentMethods(customerId)).find((method) => method.id === defaultId) ?? null
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
        metadata: params.metadata,
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
      const schedule = await stripe.subscriptionSchedules.create({
        from_subscription: params.providerSubscriptionId,
      });
      await stripe.subscriptionSchedules.update(schedule.id, {
        phases: [
          {
            items: [{ price: item.price.id, quantity: item.quantity ?? 1 }],
            start_date: schedule.phases?.[0]?.start_date,
            end_date: item.current_period_end,
          },
          { items: [{ price: params.productId, quantity: params.quantity ?? 1 }] },
        ],
      });
      return { providerOperationId: schedule.id };
    }
    const updated = await stripe.subscriptions.update(params.providerSubscriptionId, {
      items: [{ id: item.id, price: params.productId, quantity: params.quantity ?? 1 }],
      proration_behavior: "always_invoice",
      payment_behavior: "pending_if_incomplete",
    });
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
        proration_behavior: params.effectiveAt === "next_billing_date" ? "none" : "always_invoice",
      },
    });
    const price = await stripe.prices.retrieve(params.productId);
    return {
      totalAmount: invoice.total ?? 0,
      settlementAmount: invoice.amount_due ?? 0,
      currency: invoice.currency,
      lineItems: invoice.lines.data.map((line) => ({
        productId: params.productId,
        name: line.description ?? "Subscription change",
        unitPrice: line.amount ?? 0,
        quantity: line.quantity ?? 1,
        prorationFactor: 1,
        currency: line.currency ?? invoice.currency,
        tax: 0,
        subtotal: line.amount ?? 0,
      })),
      effectiveAt:
        params.effectiveAt === "next_billing_date"
          ? new Date(item.current_period_end * 1000).toISOString()
          : new Date().toISOString(),
      recurringAmount: price.unit_amount ?? 0,
      recurringCurrency: price.currency,
      nextBillingDate: new Date(item.current_period_end * 1000).toISOString(),
    };
  }
}
