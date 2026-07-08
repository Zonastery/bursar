import DodoPayments from "dodopayments";
import type { PaymentProvider, ResolveUserCallback, ProviderLogger } from "../types.js";
import type {
  CheckoutParams,
  PortalParams,
  UpdatePaymentMethodParams,
  PaymentMethodSetupParams,
  CreateCustomerParams,
  PaymentMethodInfo,
  WebhookRequest,
} from "../types.js";
import type { BillingManager } from "../../billing/billing-manager.js";
import { handleDodoBillingEvent } from "./event-mapper.js";

export class DodoProvider implements PaymentProvider {
  readonly provider = "dodo" as const;

  constructor(
    private getClient: () => DodoPayments,
    private config: { webhookKey: string; setupProductId?: string },
    private bm: BillingManager,
    private resolveUser?: ResolveUserCallback,
    private logger?: ProviderLogger,
  ) {}

  async createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string }> {
    const client = this.getClient();
    const session = await client.checkoutSessions.create({
      product_cart: [{ product_id: params.productId, quantity: params.quantity ?? 1 }],
      ...(params.customerId ? { customer: { customer_id: params.customerId } } : {}),
      return_url: params.returnUrl,
      cancel_url: params.cancelUrl,
      metadata: params.metadata,
    });
    if (!session.checkout_url) throw new Error("Checkout session returned no URL");
    return { url: session.checkout_url };
  }

  async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
    const client = this.getClient();
    const session = await client.customers.customerPortal.create(params.customerId, {
      return_url: params.returnUrl,
    });
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

    await handleDodoBillingEvent(type, data, userId, metadata, this.bm, this.logger);
    return { received: true };
  }

  async cancelSubscription(subscriptionId: string): Promise<void> {
    const client = this.getClient();
    await client.subscriptions.update(subscriptionId, {
      cancel_at_next_billing_date: true,
      cancel_reason: "cancelled_by_customer",
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
    const client = this.getClient();
    const response = await client.subscriptions.updatePaymentMethod(params.subscriptionId, {
      payment_method: { type: "new", return_url: params.returnUrl },
    });
    if (!response.payment_link) throw new Error("Failed to create payment method update session");
    return { url: response.payment_link };
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
      cancel_url: params.cancelUrl ?? params.returnUrl,
      metadata: { purpose: "setup_payment_method" },
    });
    if (!session.checkout_url) throw new Error("Checkout session returned no URL");
    return { url: session.checkout_url };
  }

  async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
    const client = this.getClient();
    const response = await client.customers.retrievePaymentMethods(customerId);
    return response.items.map((item) => ({
      id: item.payment_method_id,
      last4: item.card?.last4_digits ?? "",
      brand: item.card?.card_network ?? "",
      expiryMonth: Number(item.card?.expiry_month ?? 0),
      expiryYear: Number(item.card?.expiry_year ?? 0),
    }));
  }

  async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
    const client = this.getClient();
    const customer = await client.customers.create({
      email: params.email,
      name: params.name,
      metadata: params.metadata,
    });
    return { customerId: customer.customer_id };
  }

  async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
    const client = this.getClient();
    const payment = await client.payments.retrieve(providerPaymentId);
    return payment.invoice_url ? { url: payment.invoice_url } : null;
  }
}
