import Stripe from "stripe";
import type { CheckoutPaymentStatus, PaymentProvider } from "../types.js";
import { type ProviderLogger, normalizeProviderLogger } from "../types.js";
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

  async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
    const stripe = this.getStripe();
    const methods = await stripe.paymentMethods.list({
      customer: customerId,
      type: "card",
    });
    return methods.data.map((pm) => ({
      id: pm.id,
      last4: pm.card?.last4 ?? "",
      brand: pm.card?.brand ?? "unknown",
      expiryMonth: pm.card?.exp_month ?? 0,
      expiryYear: pm.card?.exp_year ?? 0,
    }));
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

  async changePlan(_params: ChangePlanParams): Promise<void> {
    throw new Error("StripeProvider.changePlan not implemented");
  }

  async previewChangePlan(_params: PreviewChangePlanParams): Promise<ChangePlanPreview> {
    throw new Error("StripeProvider.previewChangePlan not implemented");
  }
}
