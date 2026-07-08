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
import { handleDodoBillingEvent } from "../dodo/event-mapper.js";

export class MockPaymentProvider implements PaymentProvider {
  readonly provider = "dodo" as const;

  constructor(
    private bm: BillingManager,
    private resolveUser?: ResolveUserCallback,
    private logger?: ProviderLogger,
  ) {}

  async createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string }> {
    return { url: params.returnUrl };
  }

  async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
    return { url: params.returnUrl };
  }

  async createUpdatePaymentMethodSession(
    params: UpdatePaymentMethodParams,
  ): Promise<{ url: string }> {
    return { url: params.returnUrl };
  }

  async createPaymentMethodSetupSession(
    params: PaymentMethodSetupParams,
  ): Promise<{ url: string }> {
    return { url: params.returnUrl };
  }

  async cancelSubscription(_subscriptionId: string): Promise<void> {}

  async reactivateSubscription(_subscriptionId: string): Promise<void> {}

  async listPaymentMethods(_customerId: string): Promise<PaymentMethodInfo[]> {
    return [];
  }

  async createCustomer(_params: CreateCustomerParams): Promise<{ customerId: string }> {
    return { customerId: `mock_cus_${Date.now()}` };
  }

  async getInvoiceUrl(_providerPaymentId: string): Promise<{ url: string } | null> {
    return { url: "https://example.com/invoice" };
  }

  async handleWebhook(req: WebhookRequest): Promise<{ received: boolean; retryable?: boolean }> {
    let payload: Record<string, unknown> | null;
    try {
      payload = JSON.parse(req.rawBody);
    } catch {
      return { received: false, retryable: false };
    }
    if (!payload || typeof payload !== "object") {
      return { received: false, retryable: false };
    }

    const data = (payload.data ?? {}) as Record<string, unknown>;
    const metadata = (data.metadata ?? {}) as Record<string, string>;
    let userId: string | null = metadata.userId ?? null;

    if (!userId && this.resolveUser) {
      userId = await this.resolveUser(data, metadata);
    }

    await handleDodoBillingEvent(
      String(payload.type),
      data,
      userId,
      metadata,
      this.bm,
      this.logger,
    );

    return { received: true };
  }
}
