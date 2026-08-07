export interface WebhookRequest {
  rawBody: string;
  headers: Record<string, string>;
}

export interface WebhookResult {
  received: boolean;
  retryable: boolean;
  provider: string;
  eventId: string | null;
  eventType: string | null;
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

export interface ProviderUrlResult {
  url: string;
}

export interface CreateCustomerResult {
  customerId: string;
}

export interface CheckoutSessionResult extends ProviderUrlResult {
  customerId?: string;
  providerSessionId?: string;
}

export interface CheckoutSessionStatus {
  paymentStatus: CheckoutPaymentStatus;
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
  | "succeeded"
  | "processing"
  | "failed"
  | "requires_customer_action"
  | "requires_payment_method";

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

export interface ResolveIdentityInput {
  provider: string;
  providerEventType: string;
  normalizedEventType: string | null;
  customerId: string | null;
  email: string | null;
  metadata: Record<string, string>;
  successful: boolean;
  checkoutKind: "subscription" | "credit_topup" | null;
}

export type ResolveUserCallback = (input: ResolveIdentityInput) => Promise<string | null>;

export { noopLogger, normalizeLogger as normalizeProviderLogger } from "../shared/logger.js";
export type {
  Logger as ProviderLogger,
  NormalizedLogger as NormalizedProviderLogger,
} from "../shared/logger.js";

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
    | "prorated_immediately"
    | "full_immediately"
    | "difference_immediately"
    | "do_not_bill";
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

export interface ChangePlanResult {
  providerOperationId?: string;
}

export interface PaymentProvider {
  readonly provider: string;

  /** Retrieve the provider state for a checkout session, or null if it no longer exists. */
  getCheckoutSessionStatus?(providerSessionId: string): Promise<CheckoutSessionStatus | null>;

  createCheckoutSession(params: CheckoutParams): Promise<CheckoutSessionResult>;

  createCustomerPortalSession?(params: PortalParams): Promise<ProviderUrlResult>;

  createUpdatePaymentMethodSession?(params: UpdatePaymentMethodParams): Promise<ProviderUrlResult>;

  createPaymentMethodSetupSession?(params: PaymentMethodSetupParams): Promise<ProviderUrlResult>;

  createCustomer?(params: CreateCustomerParams): Promise<CreateCustomerResult>;

  handleWebhook(req: WebhookRequest): Promise<WebhookResult>;

  cancelSubscription?(subscriptionId: string, idempotencyKey?: string): Promise<void>;

  reactivateSubscription?(subscriptionId: string, idempotencyKey?: string): Promise<void>;

  /** Removes a pending plan switch while retaining the current subscription. */
  cancelScheduledPlanChange?(
    subscriptionId: string,
    providerOperationId?: string | null,
    idempotencyKey?: string,
  ): Promise<void>;

  listPaymentMethods?(customerId: string): Promise<PaymentMethodInfo[]>;

  previewSavedPaymentCharge?(params: SavedPaymentChargeParams): Promise<SavedPaymentChargeQuote>;

  chargeSavedPaymentMethod?(params: SavedPaymentChargeParams): Promise<SavedPaymentChargeResult>;

  getInvoiceUrl?(providerPaymentId: string): Promise<ProviderUrlResult | null>;

  changePlan?(params: ChangePlanParams): Promise<ChangePlanResult | void>;

  previewChangePlan?(params: PreviewChangePlanParams): Promise<ChangePlanPreview>;
}
