import type DodoPayments from "dodopayments";

/**
 * The public Dodo SDK surface used by Bursar.
 *
 * This structural boundary prevents compatible Dodo SDK installations from
 * becoming nominally incompatible through private class and APIPromise fields.
 */
type DodoCheckoutSession = Awaited<ReturnType<DodoPayments["checkoutSessions"]["create"]>>;
type DodoCheckoutSessionStatus = Awaited<ReturnType<DodoPayments["checkoutSessions"]["retrieve"]>>;
type DodoCheckoutPreview = Awaited<ReturnType<DodoPayments["checkoutSessions"]["preview"]>>;
type DodoPayment = Awaited<ReturnType<DodoPayments["payments"]["retrieve"]>>;
type DodoPlanChangePreview = Awaited<
  ReturnType<DodoPayments["subscriptions"]["previewChangePlan"]>
>;
type DodoPaymentMethodUpdate = Awaited<
  ReturnType<DodoPayments["subscriptions"]["updatePaymentMethod"]>
>;
type DodoSubscription = Awaited<ReturnType<DodoPayments["subscriptions"]["update"]>>;
type DodoCustomer = Awaited<ReturnType<DodoPayments["customers"]["create"]>>;
type DodoCustomerPortalSession = Awaited<
  ReturnType<DodoPayments["customers"]["customerPortal"]["create"]>
>;
type DodoPaymentMethods = Awaited<ReturnType<DodoPayments["customers"]["retrievePaymentMethods"]>>;
type DodoMutationRequestOptions = Pick<
  NonNullable<Parameters<DodoPayments["checkoutSessions"]["create"]>[1]>,
  "headers"
>;

type DodoSdkWebhookPayload = ReturnType<DodoPayments["webhooks"]["unwrap"]>;

/**
 * Payload returned by an official Dodo adapter after signature verification.
 *
 * This stays SDK-derived so an actual `DodoPayments` client is structurally
 * assignable to `DodoClient` without casts or weakened return types.
 */
export type DodoWebhookPayload = DodoSdkWebhookPayload;

/** Common verified envelope shared by Dodo's framework-specific adapters. */
export interface DodoWebhookEnvelope<TData> {
  data: TData;
  timestamp: string | Date;
  type: string;
}

export interface DodoClient {
  webhooks: {
    unwrap(
      body: string,
      options: { headers: Record<string, string>; key?: string },
    ): DodoWebhookPayload;
  };
  checkoutSessions: {
    create(
      body: Parameters<DodoPayments["checkoutSessions"]["create"]>[0],
      options?: DodoMutationRequestOptions,
    ): Promise<DodoCheckoutSession>;
    preview(
      body: Parameters<DodoPayments["checkoutSessions"]["preview"]>[0],
    ): Promise<DodoCheckoutPreview>;
    retrieve(
      sessionId: Parameters<DodoPayments["checkoutSessions"]["retrieve"]>[0],
    ): Promise<DodoCheckoutSessionStatus>;
  };
  customers: {
    create(
      body: Parameters<DodoPayments["customers"]["create"]>[0],
      options?: DodoMutationRequestOptions,
    ): Promise<DodoCustomer>;
    customerPortal: {
      create(
        customerId: Parameters<DodoPayments["customers"]["customerPortal"]["create"]>[0],
        body: Parameters<DodoPayments["customers"]["customerPortal"]["create"]>[1],
      ): Promise<DodoCustomerPortalSession>;
    };
    retrievePaymentMethods(
      customerId: Parameters<DodoPayments["customers"]["retrievePaymentMethods"]>[0],
    ): Promise<DodoPaymentMethods>;
  };
  payments: {
    retrieve(paymentId: Parameters<DodoPayments["payments"]["retrieve"]>[0]): Promise<DodoPayment>;
  };
  subscriptions: {
    cancelChangePlan(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["cancelChangePlan"]>[0],
      options?: DodoMutationRequestOptions,
    ): Promise<void>;
    changePlan(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["changePlan"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["changePlan"]>[1],
      options?: DodoMutationRequestOptions,
    ): Promise<void>;
    previewChangePlan(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["previewChangePlan"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["previewChangePlan"]>[1],
    ): Promise<DodoPlanChangePreview>;
    update(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["update"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["update"]>[1],
      options?: DodoMutationRequestOptions,
    ): Promise<DodoSubscription>;
    updatePaymentMethod(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["updatePaymentMethod"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["updatePaymentMethod"]>[1],
    ): Promise<DodoPaymentMethodUpdate>;
  };
}
