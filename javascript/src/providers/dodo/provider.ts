import { NotFoundError } from "dodopayments";
import type { CheckoutSessionCreateParams } from "dodopayments/resources/checkout-sessions";
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
  ChangePlanLineItem,
  SavedPaymentChargeParams,
  SavedPaymentChargeResult,
  SavedPaymentChargeQuote,
  WebhookResult,
} from "../types.js";
import type { BillingEventSink } from "../../bursar.js";
import { ProviderResponseError } from "../../errors.js";
import { persistedDiagnosticSummary } from "../../shared/diagnostics.js";
import { requireStableKey } from "../../shared/idempotency.js";
import type { DodoClient, DodoWebhookPayload } from "./client-contract.js";
import { dodoBillingEventId, handleDodoBillingEvent } from "./event-mapper.js";

export interface DodoWebhookProcessorOptions {
  eventSink: BillingEventSink;
  logger?: ProviderLogger | null;
}

export interface DodoProviderOptions extends DodoWebhookProcessorOptions {
  getClient: () => DodoClient;
  webhookKey: string;
  setupProductId?: string;
}

const BURSAR_METADATA_KEYS = new Set([
  "bursar_account_id",
  "plan_slug",
  "billing_interval",
  "credits",
  "checkout_intent_id",
]);

// Dodo's SDK exposes RequestOptions.idempotencyKey, but its client does not
// configure the internal header name. Send the API header
// explicitly so retries reach Dodo with the caller-stable operation key.
const DODO_IDEMPOTENCY_HEADER = "Idempotency-Key";

function dodoIdempotencyOptions(key: string) {
  return {
    headers: {
      [DODO_IDEMPOTENCY_HEADER]: requireStableKey(key),
    },
  };
}

function normalizeMetadata(value: unknown): Record<string, string> {
  if (value === null || value === undefined) return {};
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Dodo webhook metadata must be an object");
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => {
      if (
        (typeof item !== "string" && typeof item !== "number" && typeof item !== "boolean") ||
        (typeof item === "number" && !Number.isFinite(item))
      ) {
        throw new TypeError(`Dodo webhook metadata.${key} must be a scalar value`);
      }
      if (BURSAR_METADATA_KEYS.has(key) && typeof item !== "string") {
        throw new TypeError(`Dodo webhook metadata.${key} must be a string`);
      }
      return [key, String(item)];
    }),
  );
}

function requireProviderText(value: unknown, operation: string, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ProviderResponseError("dodo", operation, { details: { field } });
  }
  return value;
}

function requireProviderInteger(
  value: unknown,
  operation: string,
  field: string,
  options: { min?: number; max?: number } = {},
): number {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string" && /^\d+$/.test(value)
        ? Number(value)
        : Number.NaN;
  if (
    !Number.isSafeInteger(parsed) ||
    parsed < (options.min ?? 0) ||
    (options.max !== undefined && parsed > options.max)
  ) {
    throw new ProviderResponseError("dodo", operation, { details: { field } });
  }
  return parsed;
}

/**
 * Maps an already verified Dodo payload into Bursar's provider-neutral billing
 * lifecycle. Framework integrations use this after the official Dodo adapter
 * has performed request parsing, signature verification, and schema validation.
 */
export class DodoWebhookProcessor {
  private readonly sink: BillingEventSink;
  private readonly logger: ReturnType<typeof normalizeProviderLogger>;

  constructor(options: DodoWebhookProcessorOptions) {
    this.sink = options.eventSink;
    this.logger = normalizeProviderLogger(options.logger);
  }

  async handle(payload: DodoWebhookPayload): Promise<WebhookResult> {
    if (typeof payload.data !== "object" || payload.data === null || Array.isArray(payload.data)) {
      throw new TypeError("Dodo webhook data must be an object");
    }
    const data = payload.data as Record<string, unknown>;
    const type = payload.type;
    const metadata = normalizeMetadata(data.metadata);
    const accountId = metadata.bursar_account_id ?? null;

    await handleDodoBillingEvent(payload, accountId, metadata, this.sink, this.logger);
    return {
      received: true,
      retryable: false,
      provider: "dodo",
      eventId: dodoBillingEventId(payload),
      eventType: type,
    };
  }
}

export class DodoProvider implements PaymentProvider {
  readonly provider = "dodo" as const;
  private readonly getClient: () => DodoClient;
  private readonly config: { webhookKey: string; setupProductId?: string };
  private readonly logger: ReturnType<typeof normalizeProviderLogger>;
  private readonly webhookProcessor: DodoWebhookProcessor;

  constructor(options: DodoProviderOptions) {
    if (!options.webhookKey.trim()) throw new TypeError("webhookKey must not be empty");
    this.getClient = options.getClient;
    this.config = {
      webhookKey: options.webhookKey,
      ...(options.setupProductId ? { setupProductId: options.setupProductId } : {}),
    };
    this.logger = normalizeProviderLogger(options.logger);
    this.webhookProcessor = new DodoWebhookProcessor(options);
  }

  async createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string; providerSessionId?: string }> {
    const requestOptions = dodoIdempotencyOptions(params.idempotencyKey);
    this.logger.info("[DodoProvider] createCheckoutSession", {
      productId: params.productId,
      type: params.type,
      hasAccountId: Boolean(params.accountId),
    });
    const client = this.getClient();
    const quantity = requireProviderInteger(
      params.quantity ?? 1,
      "createCheckoutSession",
      "quantity",
      { min: 1 },
    );
    const body: CheckoutSessionCreateParams = {
      product_cart: [{ product_id: params.productId, quantity }],
      customer: params.customerId
        ? { customer_id: params.customerId }
        : params.email
          ? { email: params.email }
          : undefined,
      return_url: params.returnUrl,
      cancel_url: params.cancelUrl,
      metadata: { ...params.metadata, bursar_account_id: params.accountId },
      ...(params.email ? { feature_flags: { allow_customer_editing_email: false } } : {}),
    };
    const session = await client.checkoutSessions.create(body, requestOptions);
    return {
      url: requireProviderText(session.checkout_url, "createCheckoutSession", "checkout_url"),
      providerSessionId: requireProviderText(
        session.session_id,
        "createCheckoutSession",
        "session_id",
      ),
    };
  }

  async getCheckoutSessionStatus(providerSessionId: string): Promise<{
    paymentStatus: CheckoutPaymentStatus;
  } | null> {
    const client = this.getClient();
    try {
      const session = await client.checkoutSessions.retrieve(providerSessionId);
      return { paymentStatus: session.payment_status ?? null };
    } catch (error) {
      if (error instanceof NotFoundError) return null;
      throw error;
    }
  }

  async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
    const client = this.getClient();
    const session = await client.customers.customerPortal.create(params.customerId, {
      return_url: params.returnUrl,
    });
    return {
      url: requireProviderText(session.link, "createCustomerPortalSession", "link"),
    };
  }

  async handleWebhook(req: WebhookRequest): Promise<WebhookResult> {
    let payload: DodoWebhookPayload;
    try {
      payload = this.getClient().webhooks.unwrap(req.rawBody, {
        headers: req.headers,
        key: this.config.webhookKey,
      });
    } catch (error) {
      this.logger.warn("[DodoProvider] webhook verification failed", {
        error: persistedDiagnosticSummary(error, "webhook_verification_failed"),
      });
      return {
        received: false,
        retryable: false,
        provider: this.provider,
        eventId: null,
        eventType: null,
      };
    }

    return this.handleVerifiedWebhook(payload);
  }

  /** Process a payload already verified and parsed by an official Dodo adapter. */
  async handleVerifiedWebhook(payload: DodoWebhookPayload): Promise<WebhookResult> {
    return this.webhookProcessor.handle(payload);
  }

  async cancelSubscription(subscriptionId: string, idempotencyKey: string): Promise<void> {
    const requestOptions = dodoIdempotencyOptions(idempotencyKey);
    const client = this.getClient();
    await client.subscriptions.update(
      subscriptionId,
      {
        cancel_at_next_billing_date: true,
      },
      requestOptions,
    );
  }

  async reactivateSubscription(subscriptionId: string, idempotencyKey: string): Promise<void> {
    const requestOptions = dodoIdempotencyOptions(idempotencyKey);
    const client = this.getClient();
    await client.subscriptions.update(
      subscriptionId,
      {
        cancel_at_next_billing_date: false,
      },
      requestOptions,
    );
  }

  async cancelScheduledPlanChange(
    subscriptionId: string,
    _providerOperationId: string | null | undefined,
    idempotencyKey: string,
  ): Promise<void> {
    const requestOptions = dodoIdempotencyOptions(idempotencyKey);
    const client = this.getClient();
    await client.subscriptions.cancelChangePlan(subscriptionId, requestOptions);
  }

  async createUpdatePaymentMethodSession(
    params: UpdatePaymentMethodParams,
  ): Promise<{ url: string }> {
    const client = this.getClient();
    const response = await client.subscriptions.updatePaymentMethod(params.subscriptionId, {
      payment_method: { type: "new", return_url: params.returnUrl },
    });
    return {
      url: requireProviderText(
        response.payment_link,
        "createUpdatePaymentMethodSession",
        "payment_link",
      ),
    };
  }

  async createPaymentMethodSetupSession(
    params: PaymentMethodSetupParams,
  ): Promise<{ url: string }> {
    const productId = params.productId ?? this.config.setupProductId;
    if (!productId) throw new TypeError("setupProductId is required for payment method setup");
    const client = this.getClient();
    const session = await client.checkoutSessions.create({
      product_cart: [{ product_id: productId, quantity: 1 }],
      customer: { customer_id: params.customerId },
      return_url: params.returnUrl,
      metadata: { purpose: "setup_payment_method" },
      subscription_data: { on_demand: { mandate_only: true } },
    });
    return {
      url: requireProviderText(
        session.checkout_url,
        "createPaymentMethodSetupSession",
        "checkout_url",
      ),
    };
  }

  async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
    const client = this.getClient();
    const { items } = await client.customers.retrievePaymentMethods(customerId);
    const methods = items
      .filter((method) => method.payment_method === "card" && method.recurring_enabled)
      .map((method) => {
        if (method.card === null || method.card === undefined) {
          throw new ProviderResponseError("dodo", "listPaymentMethods", {
            details: { field: "card" },
          });
        }
        const last4 = requireProviderText(
          method.card.last4_digits,
          "listPaymentMethods",
          "card.last4_digits",
        );
        if (!/^\d{4}$/.test(last4)) {
          throw new ProviderResponseError("dodo", "listPaymentMethods", {
            details: { field: "card.last4_digits" },
          });
        }
        return {
          id: requireProviderText(
            method.payment_method_id,
            "listPaymentMethods",
            "payment_method_id",
          ),
          last4,
          brand: requireProviderText(
            method.card.card_network,
            "listPaymentMethods",
            "card.card_network",
          ),
          expiryMonth: requireProviderInteger(
            method.card.expiry_month,
            "listPaymentMethods",
            "card.expiry_month",
            { min: 1, max: 12 },
          ),
          expiryYear: requireProviderInteger(
            method.card.expiry_year,
            "listPaymentMethods",
            "card.expiry_year",
            { min: 1 },
          ),
        };
      });
    const deduplicated = deduplicatePaymentMethods(methods);
    if (deduplicated.length === 1) deduplicated[0]!.isDefault = true;
    return deduplicated;
  }

  async previewSavedPaymentCharge(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeQuote> {
    const preview = await this.getClient().checkoutSessions.preview({
      product_cart: [{ product_id: params.productId, quantity: params.quantity }],
      customer: { customer_id: params.customerId },
    });
    return {
      amountMinor: requireProviderInteger(
        preview.current_breakup.total_amount,
        "previewSavedPaymentCharge",
        "current_breakup.total_amount",
      ),
      taxMinor:
        preview.current_breakup.tax == null
          ? null
          : requireProviderInteger(
              preview.current_breakup.tax,
              "previewSavedPaymentCharge",
              "current_breakup.tax",
            ),
      currency: requireProviderText(preview.currency, "previewSavedPaymentCharge", "currency"),
    };
  }

  async chargeSavedPaymentMethod(
    params: SavedPaymentChargeParams,
  ): Promise<SavedPaymentChargeResult> {
    const requestOptions = dodoIdempotencyOptions(params.idempotencyKey);
    const client = this.getClient();
    const session = await client.checkoutSessions.create(
      {
        product_cart: [{ product_id: params.productId, quantity: params.quantity }],
        customer: { customer_id: params.customerId },
        payment_method_id: params.paymentMethodId,
        confirm: true,
        return_url: params.returnUrl,
        metadata: params.metadata,
      },
      requestOptions,
    );
    const paymentId = requireProviderText(
      session.payment_id,
      "chargeSavedPaymentMethod",
      "payment_id",
    );
    const payment = await client.payments.retrieve(paymentId);
    if (payment.status == null) {
      throw new ProviderResponseError("dodo", "chargeSavedPaymentMethod", {
        details: { field: "status" },
      });
    }
    return {
      providerPaymentId: requireProviderText(
        payment.payment_id,
        "chargeSavedPaymentMethod",
        "payment_id",
      ),
      status: payment.status,
      amountMinor: requireProviderInteger(
        payment.total_amount,
        "chargeSavedPaymentMethod",
        "total_amount",
      ),
      currency: requireProviderText(payment.currency, "chargeSavedPaymentMethod", "currency"),
    };
  }

  async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
    const client = this.getClient();
    const requestOptions = dodoIdempotencyOptions(params.idempotencyKey);
    const customer = await client.customers.create(
      {
        email: params.email,
        name: params.name,
        ...(params.metadata ? { metadata: params.metadata } : {}),
      },
      requestOptions,
    );
    return {
      customerId: requireProviderText(customer.customer_id, "createCustomer", "customer_id"),
    };
  }

  async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
    const client = this.getClient();
    const payment = await client.payments.retrieve(providerPaymentId);
    return payment.invoice_url
      ? {
          url: requireProviderText(payment.invoice_url, "getInvoiceUrl", "invoice_url"),
        }
      : null;
  }

  async changePlan(params: ChangePlanParams): Promise<{ providerOperationId?: string }> {
    const requestOptions = dodoIdempotencyOptions(params.idempotencyKey);
    const client = this.getClient();
    await client.subscriptions.changePlan(
      params.providerSubscriptionId,
      {
        product_id: params.productId,
        proration_billing_mode: params.prorationBillingMode,
        quantity: params.quantity ?? 1,
        ...(params.effectiveAt ? { effective_at: params.effectiveAt } : {}),
        ...(params.onPaymentFailure ? { on_payment_failure: params.onPaymentFailure } : {}),
        ...(params.metadata ? { metadata: params.metadata } : {}),
      },
      requestOptions,
    );
    return {};
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
        if (
          !item.product_id ||
          item.unit_price == null ||
          item.quantity == null ||
          item.proration_factor == null ||
          !item.currency
        ) {
          throw new ProviderResponseError("dodo", "previewChangePlan", {
            details: { field: "immediate_charge.line_items" },
          });
        }
        const name = item.name ?? item.description;
        if (!name?.trim()) {
          throw new ProviderResponseError("dodo", "previewChangePlan", {
            details: { field: "immediate_charge.line_items.name" },
          });
        }
        lineItems.push({
          productId: item.product_id,
          name,
          unitPrice: item.unit_price,
          quantity: item.quantity,
          prorationFactor: item.proration_factor,
          currency: item.currency,
          tax: item.tax ?? 0,
          subtotal: Math.round(item.unit_price * item.quantity * item.proration_factor),
        });
      }
    }

    return {
      totalAmount: response.immediate_charge.summary.total_amount,
      settlementAmount: response.immediate_charge.summary.settlement_amount,
      currency: response.immediate_charge.summary.settlement_currency,
      lineItems,
      effectiveAt: response.immediate_charge.effective_at,
      recurringAmount: response.new_plan?.recurring_pre_tax_amount ?? undefined,
      recurringCurrency: response.new_plan?.currency ?? undefined,
      nextBillingDate: response.new_plan?.next_billing_date ?? undefined,
      // The dialog shows settlement_amount (after provider credits), so tax
      // must use the matching settlement_tax field. `tax` is the pre-
      // settlement tax and can be much larger than the amount actually due.
      taxAmount:
        response.immediate_charge.summary.settlement_tax ??
        response.immediate_charge.summary.tax ??
        undefined,
      customerCredits: response.immediate_charge.summary.customer_credits,
    };
  }
}
