export interface WebhookRequest {
  rawBody: string;
  headers: Record<string, string>;
}

export interface CheckoutParams {
  userId?: string;
  customerId?: string;
  email?: string;
  productId: string;
  type: "subscription" | "credit_pack";
  quantity?: number;
  returnUrl: string;
  cancelUrl: string;
  metadata: Record<string, string>;
  /** Provider-level idempotency key. Prevents duplicate checkout sessions on
   *  network retries or double-clicks. Generated server-side per request. */
  idempotencyKey?: string;
}

export interface PortalParams {
  customerId: string;
  returnUrl: string;
}

export interface UpdatePaymentMethodParams {
  customerId: string;
  subscriptionId: string;
  returnUrl: string;
  productId?: string;
}

export interface PaymentMethodSetupParams {
  customerId: string;
  returnUrl: string;
  cancelUrl?: string;
  productId?: string;
}

export interface CreateCustomerParams {
  email: string;
  name: string;
  metadata: Record<string, string>;
}

export interface PaymentMethodInfo {
  id: string;
  last4: string;
  brand: string;
  expiryMonth: number;
  expiryYear: number;
  isDefault?: boolean;
}

export type SavedPaymentChargeStatus =
  "succeeded" | "processing" | "failed" | "requires_customer_action" | "requires_payment_method";

export interface SavedPaymentChargeParams {
  customerId: string;
  paymentMethodId: string;
  productId: string;
  quantity: number;
  metadata: Record<string, string>;
  idempotencyKey: string;
  returnUrl?: string;
}

export interface SavedPaymentChargeResult {
  providerPaymentId?: string;
  status: SavedPaymentChargeStatus;
  actionUrl?: string;
  amountMinor?: number;
  currency?: string;
}

export interface SavedPaymentChargeQuote {
  amountMinor: number;
  currency: string;
  taxMinor?: number | null;
  expiresAt?: string | null;
}

/** Provider APIs can return duplicate records for the same visible card. */
export function deduplicatePaymentMethods(methods: PaymentMethodInfo[]): PaymentMethodInfo[] {
  const seen = new Set<string>();
  return methods.filter((method) => {
    const key = [
      method.brand.trim().toLowerCase(),
      method.last4,
      method.expiryMonth,
      method.expiryYear,
    ].join(":");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export type ResolveUserCallback = (
  data: Record<string, unknown>,
  metadata: Record<string, string>,
  eventType?: string,
) => Promise<string | null>;

export interface ProviderLogger {
  debug?: (msg: string, ctx?: Record<string, unknown>) => void;
  info?: (msg: string, ctx?: Record<string, unknown>) => void;
  warn?: (msg: string, ctx?: Record<string, unknown>) => void;
  error?: (msg: string, ctx?: Record<string, unknown>) => void;
}

export type NormalizedProviderLogger = Required<ProviderLogger>;

export const noopLogger: NormalizedProviderLogger = {
  debug: () => {},
  info: () => {},
  warn: () => {},
  error: () => {},
};

export function normalizeProviderLogger(logger?: ProviderLogger | null): NormalizedProviderLogger {
  return {
    debug: logger?.debug ?? noopLogger.debug,
    info: logger?.info ?? noopLogger.info,
    warn: logger?.warn ?? noopLogger.warn,
    error: logger?.error ?? noopLogger.error,
  };
}

export type CheckoutPaymentStatus =
  | null
  | "succeeded"
  | "failed"
  | "cancelled"
  | "processing"
  | "requires_customer_action"
  | "requires_merchant_action"
  | "requires_payment_method"
  | "requires_confirmation"
  | "requires_capture"
  | "partially_captured"
  | "partially_captured_and_capturable";

export interface ChangePlanParams {
  providerSubscriptionId: string;
  productId: string;
  prorationBillingMode:
    "prorated_immediately" | "full_immediately" | "difference_immediately" | "do_not_bill";
  effectiveAt?: "immediately" | "next_billing_date";
  onPaymentFailure?: "prevent_change" | "apply_change";
  quantity?: number;
  metadata?: Record<string, string>;
  idempotencyKey?: string;
}
export interface PlanSelection {
  planId: string;
  interval: "month" | "year";
}

export interface PreviewChangePlanParams {
  providerSubscriptionId: string;
  productId: string;
  prorationBillingMode: ChangePlanParams["prorationBillingMode"];
  effectiveAt?: "immediately" | "next_billing_date";
  quantity?: number;
}

export interface ChangePlanLineItem {
  productId: string;
  name: string;
  unitPrice: number;
  quantity: number;
  prorationFactor: number;
  currency: string;
  tax: number;
  subtotal: number;
}

export interface ChangePlanPreview {
  totalAmount: number;
  settlementAmount: number;
  currency: string;
  lineItems: ChangePlanLineItem[];
  effectiveAt: string;
  recurringAmount?: number;
  recurringCurrency?: string;
  nextBillingDate?: string;
  taxAmount?: number;
  customerCredits?: number;
}

export interface PaymentProvider {
  readonly provider: "stripe" | "dodo" | "mock";

  /** Retrieve the provider state for a checkout session, or null if it no longer exists. */
  getCheckoutSessionStatus?(providerSessionId: string): Promise<{
    paymentStatus: CheckoutPaymentStatus;
  } | null>;

  createCheckoutSession(
    params: CheckoutParams,
  ): Promise<{ url: string; customerId?: string; providerSessionId?: string }>;

  createCustomerPortalSession(params: PortalParams): Promise<{ url: string }>;

  createUpdatePaymentMethodSession(params: UpdatePaymentMethodParams): Promise<{ url: string }>;

  createPaymentMethodSetupSession(params: PaymentMethodSetupParams): Promise<{ url: string }>;

  createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }>;

  handleWebhook(req: WebhookRequest): Promise<{ received: boolean; retryable?: boolean }>;

  cancelSubscription(subscriptionId: string, idempotencyKey?: string): Promise<void>;

  reactivateSubscription(subscriptionId: string, idempotencyKey?: string): Promise<void>;

  /** Removes a pending plan switch while retaining the current subscription. */
  cancelScheduledPlanChange?(
    subscriptionId: string,
    providerOperationId?: string | null,
    idempotencyKey?: string,
  ): Promise<void>;

  listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]>;

  getDefaultPaymentMethod?(customerId: string): Promise<PaymentMethodInfo | null>;

  previewSavedPaymentCharge?(params: SavedPaymentChargeParams): Promise<SavedPaymentChargeQuote>;

  chargeSavedPaymentMethod(params: SavedPaymentChargeParams): Promise<SavedPaymentChargeResult>;

  getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null>;

  changePlan(params: ChangePlanParams): Promise<{ providerOperationId?: string } | void>;

  previewChangePlan(params: PreviewChangePlanParams): Promise<ChangePlanPreview>;
}
