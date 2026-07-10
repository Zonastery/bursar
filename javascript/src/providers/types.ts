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
}

export type ResolveUserCallback = (
  data: Record<string, unknown>,
  metadata: Record<string, string>,
) => Promise<string | null>;

export interface ProviderLogger {
  debug?: (msg: string, ctx?: Record<string, unknown>) => void;
  warn?: (msg: string, ctx?: Record<string, unknown>) => void;
  error?: (msg: string, ctx?: Record<string, unknown>) => void;
}

export interface PaymentProvider {
  readonly provider: "stripe" | "dodo" | "mock";

  createCheckoutSession(params: CheckoutParams): Promise<{ url: string; customerId?: string }>;

  createCustomerPortalSession(params: PortalParams): Promise<{ url: string }>;

  createUpdatePaymentMethodSession(params: UpdatePaymentMethodParams): Promise<{ url: string }>;

  createPaymentMethodSetupSession(params: PaymentMethodSetupParams): Promise<{ url: string }>;

  createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }>;

  handleWebhook(req: WebhookRequest): Promise<{ received: boolean; retryable?: boolean }>;

  cancelSubscription(subscriptionId: string): Promise<void>;

  reactivateSubscription(subscriptionId: string): Promise<void>;

  listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]>;

  getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null>;
}
