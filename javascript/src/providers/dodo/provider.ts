import DodoPayments from "dodopayments";
import type { CheckoutSessionCreateParams } from "dodopayments/resources/checkout-sessions";
import type { PaymentProvider, ResolveUserCallback, ProviderLogger } from "../types.js";
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
  ChangePlanLineItem,
} from "../types.js";
import type { BillingEventSink } from "../../bursar.js";
import { handleDodoBillingEvent } from "./event-mapper.js";

export class DodoProvider implements PaymentProvider {
  readonly provider = "dodo" as const;

  constructor(
    private getClient: () => DodoPayments,
    private config: { webhookKey: string; setupProductId?: string },
    private sink: BillingEventSink,
    private resolveUser?: ResolveUserCallback,
    private logger?: ProviderLogger,
  ) {}

  async createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string; providerSessionId?: string }> {
    const client = this.getClient();
    const body: CheckoutSessionCreateParams = {
      product_cart: [{ product_id: params.productId, quantity: params.quantity ?? 1 }],
      customer: params.customerId
        ? { customer_id: params.customerId }
        : params.email
          ? { email: params.email }
          : undefined,
      return_url: params.returnUrl,
      metadata: params.metadata,
    };
    const requestOptions = params.idempotencyKey
      ? { idempotencyKey: params.idempotencyKey }
      : undefined;
    const session = await client.checkoutSessions.create(body, requestOptions);
    if (!session.checkout_url) throw new Error("Checkout session returned no URL");
    return { url: session.checkout_url, providerSessionId: session.session_id };
  }

  async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
    const client = this.getClient();
    const session = await client.customers.customerPortal.create(params.customerId, {
      return_url: params.returnUrl,
    } as Record<string, unknown>);
    return { url: session.link };
  }

  async handleWebhook(req: WebhookRequest): Promise<{ received: boolean; retryable?: boolean }> {
    const { verifyWebhookPayload } = await import("@dodopayments/core/webhook");

    let payload: { type: string; data?: unknown };
    try {
      payload = await verifyWebhookPayload({
        webhookKey: this.config.webhookKey,
        headers: req.headers,
        body: req.rawBody,
      });
    } catch {
      return { received: false, retryable: false };
    }

    const data = (payload.data ?? {}) as Record<string, unknown>;
    const type: string = payload.type;
    const metadata = (data.metadata ?? {}) as Record<string, string>;
    let userId: string | null = metadata.userId ?? null;

    if (!userId && this.resolveUser) {
      userId = await this.resolveUser(data, metadata);
    }

    await handleDodoBillingEvent(type, data, userId, metadata, this.sink, this.logger);
    return { received: true };
  }

  async cancelSubscription(subscriptionId: string): Promise<void> {
    const client = this.getClient();
    await client.subscriptions.update(subscriptionId, {
      cancel_at_next_billing_date: true,
    });
  }

  async reactivateSubscription(subscriptionId: string): Promise<void> {
    const client = this.getClient();
    await client.subscriptions.update(subscriptionId, {
      cancel_at_next_billing_date: false,
    });
  }

  async createUpdatePaymentMethodSession(
    params: UpdatePaymentMethodParams,
  ): Promise<{ url: string }> {
    const productId = params.productId ?? this.config.setupProductId;
    if (!productId) throw new Error("productId is required for payment method update");
    const client = this.getClient();
    const response = await client.checkoutSessions.create({
      product_cart: [{ product_id: productId, quantity: 1 }],
      customer: { customer_id: params.customerId },
      return_url: params.returnUrl,
      metadata: { purpose: "update_payment_method", subscription_id: params.subscriptionId },
    });
    if (!response.checkout_url) throw new Error("Failed to create payment method update session");
    return { url: response.checkout_url };
  }

  async createPaymentMethodSetupSession(
    params: PaymentMethodSetupParams,
  ): Promise<{ url: string }> {
    const productId = params.productId ?? this.config.setupProductId;
    if (!productId) throw new Error("setupProductId is required for payment method setup");
    const client = this.getClient();
    const session = await client.checkoutSessions.create({
      product_cart: [{ product_id: productId, quantity: 1 }],
      customer: { customer_id: params.customerId },
      return_url: params.returnUrl,
      metadata: { purpose: "setup_payment_method" },
    });
    if (!session.checkout_url) throw new Error("Checkout session returned no URL");
    return { url: session.checkout_url };
  }

  async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
    const client = this.getClient();
    try {
      const { items } = await client.customers.retrievePaymentMethods(customerId);
      return items
        .filter((pm) => pm.payment_method === "card" && pm.card && pm.recurring_enabled)
        .map((pm) => ({
          id: pm.payment_method_id,
          last4: pm.card!.last4_digits ?? "",
          brand: pm.card!.card_network ?? "unknown",
          expiryMonth: pm.card!.expiry_month ? Number(pm.card!.expiry_month) : 0,
          expiryYear: pm.card!.expiry_year ? Number(pm.card!.expiry_year) : 0,
        }));
    } catch {
      return [];
    }
  }

  async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
    const client = this.getClient();
    const customer = await client.customers.create({
      email: params.email,
      name: params.name,
      ...(params.metadata ? { metadata: params.metadata } : {}),
    });
    return { customerId: customer.customer_id };
  }

  async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
    const client = this.getClient();
    const payment = await client.payments.retrieve(providerPaymentId);
    return payment.payment_link ? { url: payment.payment_link } : null;
  }

  async changePlan(params: ChangePlanParams): Promise<void> {
    const client = this.getClient();
    await client.subscriptions.changePlan(params.providerSubscriptionId, {
      product_id: params.productId,
      proration_billing_mode: params.prorationBillingMode,
      quantity: params.quantity ?? 1,
      ...(params.effectiveAt ? { effective_at: params.effectiveAt } : {}),
      ...(params.onPaymentFailure ? { on_payment_failure: params.onPaymentFailure } : {}),
      ...(params.metadata ? { metadata: params.metadata } : {}),
    });
  }

  async previewChangePlan(params: PreviewChangePlanParams): Promise<ChangePlanPreview> {
    const client = this.getClient();
    const response = await client.subscriptions.previewChangePlan(params.providerSubscriptionId, {
      product_id: params.productId,
      proration_billing_mode: params.prorationBillingMode,
      quantity: params.quantity ?? 1,
      ...(params.effectiveAt ? { effective_at: params.effectiveAt } : {}),
    });

    const lineItems: ChangePlanLineItem[] = [];
    for (const item of response.immediate_charge.line_items) {
      if (item.type === "subscription") {
        lineItems.push({
          productId: item.product_id,
          name: item.name ?? item.description ?? "",
          unitPrice: item.unit_price,
          quantity: item.quantity,
          prorationFactor: item.proration_factor,
          currency: item.currency,
          tax: item.tax ?? 0,
          subtotal: 0,
        });
      }
    }

    return {
      totalAmount: response.immediate_charge.summary.total_amount,
      settlementAmount: response.immediate_charge.summary.settlement_amount,
      currency: response.immediate_charge.summary.settlement_currency,
      lineItems,
      effectiveAt: response.immediate_charge.effective_at,
    };
  }
}
