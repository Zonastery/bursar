import type { PaymentProvider, ResolveUserCallback } from "../types.js";
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
  SavedPaymentChargeParams,
  SavedPaymentChargeResult,
  SavedPaymentChargeQuote,
  WebhookResult,
} from "../types.js";
import type { BillingEventSink } from "../../bursar.js";
import { handleDodoBillingEvent } from "../dodo/event-mapper.js";

function mockIdentityInput(
  providerEventType: string,
  data: Record<string, unknown>,
  metadata: Record<string, string>,
) {
  const customer =
    typeof data.customer === "object" && data.customer !== null
      ? (data.customer as Record<string, unknown>)
      : {};
  const customerId = String(data.customer_id ?? customer.customer_id ?? "").trim() || null;
  const emailValue = customer.email;
  return {
    provider: "mock",
    providerEventType,
    normalizedEventType: providerEventType,
    customerId,
    email:
      typeof emailValue === "string" && emailValue.trim() ? emailValue.trim().toLowerCase() : null,
    metadata,
    successful:
      providerEventType === "payment.succeeded" ||
      providerEventType === "subscription.active" ||
      providerEventType === "subscription.renewed",
    checkoutKind: providerEventType.startsWith("subscription.")
      ? ("subscription" as const)
      : metadata.credits
        ? ("credit_topup" as const)
        : null,
  };
}

export class MockPaymentProvider implements PaymentProvider {
  readonly provider = "mock" as const;

  constructor(
    private sink: BillingEventSink,
    private resolveUser?: ResolveUserCallback,
    logger?: ProviderLogger | null,
  ) {
    this.logger = normalizeProviderLogger(logger);
  }

  private logger: ReturnType<typeof normalizeProviderLogger>;

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

  async cancelScheduledPlanChange(
    _subscriptionId: string,
    _providerOperationId?: string | null,
    _idempotencyKey?: string,
  ): Promise<void> {}

  async listPaymentMethods(_customerId: string): Promise<PaymentMethodInfo[]> {
    return [];
  }

  async getDefaultPaymentMethod(customerId: string): Promise<PaymentMethodInfo | null> {
    return (await this.listPaymentMethods(customerId))[0] ?? null;
  }

  async previewSavedPaymentCharge(
    _params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeQuote> {
    return { amountMinor: 0, currency: "USD" };
  }

  async chargeSavedPaymentMethod(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeResult> {
    return {
      providerPaymentId: `mock_pay_${params.idempotencyKey}`,
      status: "succeeded",
      amountMinor: 0,
      currency: "USD",
    };
  }

  async createCustomer(_params: CreateCustomerParams): Promise<{ customerId: string }> {
    return { customerId: `mock_cus_${Date.now()}` };
  }

  async getInvoiceUrl(_providerPaymentId: string): Promise<{ url: string } | null> {
    return { url: "https://example.com/invoice" };
  }

  async handleWebhook(req: WebhookRequest): Promise<WebhookResult> {
    let payload: Record<string, unknown> | null;
    try {
      payload = JSON.parse(req.rawBody);
    } catch {
      return {
        received: false,
        retryable: false,
        provider: this.provider,
        eventId: null,
        eventType: null,
      };
    }
    if (!payload || typeof payload !== "object") {
      return {
        received: false,
        retryable: false,
        provider: this.provider,
        eventId: null,
        eventType: null,
      };
    }

    const data = (payload.data ?? {}) as Record<string, unknown>;
    const metadata = (data.metadata ?? {}) as Record<string, string>;
    let userId: string | null = metadata.userId ?? null;

    if (!userId && this.resolveUser) {
      userId = await this.resolveUser(
        mockIdentityInput(String(payload.type ?? ""), data, metadata),
      );
    }

    await handleDodoBillingEvent(
      String(payload.type),
      data,
      userId,
      metadata,
      this.sink,
      this.logger,
    );

    const rawEventId =
      data.id ?? data.payment_id ?? data.subscription_id ?? data.refund_id ?? data.dispute_id;
    return {
      received: true,
      retryable: false,
      provider: this.provider,
      eventId: rawEventId == null ? null : String(rawEventId),
      eventType: typeof payload.type === "string" ? payload.type : null,
    };
  }

  async changePlan(_params: ChangePlanParams): Promise<void> {}

  async previewChangePlan(_params: PreviewChangePlanParams): Promise<ChangePlanPreview> {
    return {
      totalAmount: 0,
      settlementAmount: 0,
      currency: "USD",
      lineItems: [],
      effectiveAt: new Date().toISOString(),
    };
  }
}
