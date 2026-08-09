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
  PaymentProvider,
  ResolveUserCallback,
} from "../types.js";
import type { BillingEventSink } from "../../bursar.js";
import { assertBillingEvent } from "../../billing/types/index.js";
import { requireStableKey } from "../../shared/idempotency.js";

export interface MockPaymentProviderOptions {
  eventSink: BillingEventSink;
  resolveUser?: ResolveUserCallback;
}

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
  private readonly sink: BillingEventSink;
  private readonly resolveUser?: ResolveUserCallback;

  constructor(options: MockPaymentProviderOptions) {
    this.sink = options.eventSink;
    this.resolveUser = options.resolveUser;
  }

  async createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string }> {
    requireStableKey(params.idempotencyKey);
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

  async cancelSubscription(_subscriptionId: string, idempotencyKey: string): Promise<void> {
    requireStableKey(idempotencyKey);
  }

  async reactivateSubscription(_subscriptionId: string, idempotencyKey: string): Promise<void> {
    requireStableKey(idempotencyKey);
  }

  async cancelScheduledPlanChange(
    _subscriptionId: string,
    _providerOperationId: string | null | undefined,
    idempotencyKey: string,
  ): Promise<void> {
    requireStableKey(idempotencyKey);
  }

  async listPaymentMethods(_customerId: string): Promise<PaymentMethodInfo[]> {
    return [];
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

  async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
    const idempotencyKey = requireStableKey(params.idempotencyKey);
    return { customerId: `mock_cus_${idempotencyKey}` };
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

    const event: unknown = { ...payload, provider: this.provider };
    assertBillingEvent(event);
    let userId: string | null = event.userId ?? null;

    if (!userId && this.resolveUser) {
      const metadata = Object.fromEntries(
        Object.entries(event.metadata ?? {}).map(([key, value]) => {
          if (typeof value !== "string") {
            throw new TypeError(`mock billing event metadata.${key} must be a string`);
          }
          return [key, value];
        }),
      );
      userId = await this.resolveUser(
        mockIdentityInput(
          event.eventType,
          {
            customer_id: event.customer?.providerCustomerId,
            customer: event.customer
              ? {
                  customer_id: event.customer.providerCustomerId,
                  email: event.customer.email,
                }
              : undefined,
          },
          metadata,
        ),
      );
    }

    await this.sink.ingestBillingEvent({ ...event, ...(userId ? { userId } : {}) });
    return {
      received: true,
      retryable: false,
      provider: this.provider,
      eventId: event.eventId,
      eventType: event.eventType,
    };
  }

  async changePlan(params: ChangePlanParams): Promise<void> {
    requireStableKey(params.idempotencyKey);
  }

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
