import type DodoPayments from "dodopayments";
import type { CheckoutPaymentStatus } from "../types.js";

/**
 * The public Dodo SDK surface used by Bursar.
 *
 * This structural boundary prevents compatible Dodo SDK installations from
 * becoming nominally incompatible through private class and APIPromise fields.
 */
interface DodoCheckoutSession {
  checkout_url?: string | null;
  payment_id?: string | null;
  payment_status?: CheckoutPaymentStatus;
  session_id?: string;
}

interface DodoCheckoutPreview {
  current_breakup: {
    total_amount: number;
    tax?: number | null;
  };
  currency: string;
}

interface DodoPayment {
  payment_id: string;
  status?: string | null;
  total_amount: number;
  currency: string;
  payment_link?: string | null;
}

interface DodoPlanChangePreview {
  immediate_charge: {
    effective_at: string;
    line_items: Array<{
      type: string;
      product_id?: string;
      name?: string | null;
      description?: string | null;
      unit_price?: number;
      quantity?: number;
      proration_factor?: number;
      currency?: string;
      tax?: number | null;
    }>;
    summary: {
      total_amount: number;
      settlement_amount: number;
      settlement_currency: string;
      settlement_tax?: number | null;
      tax?: number | null;
      customer_credits: number;
    };
  };
  new_plan?: {
    recurring_pre_tax_amount?: number | null;
    currency?: string | null;
    next_billing_date?: string | null;
  } | null;
}

export interface DodoClient {
  checkoutSessions: {
    create(
      body: Parameters<DodoPayments["checkoutSessions"]["create"]>[0],
      options?: { idempotencyKey?: string },
    ): Promise<DodoCheckoutSession>;
    preview(
      body: Parameters<DodoPayments["checkoutSessions"]["preview"]>[0],
    ): Promise<DodoCheckoutPreview>;
    retrieve(
      sessionId: Parameters<DodoPayments["checkoutSessions"]["retrieve"]>[0],
    ): Promise<DodoCheckoutSession>;
  };
  customers: {
    create(
      body: Parameters<DodoPayments["customers"]["create"]>[0],
    ): Promise<{ customer_id: string }>;
    customerPortal: {
      create(
        customerId: Parameters<DodoPayments["customers"]["customerPortal"]["create"]>[0],
        body: Parameters<DodoPayments["customers"]["customerPortal"]["create"]>[1],
      ): Promise<{ link: string }>;
    };
    retrievePaymentMethods(
      customerId: Parameters<DodoPayments["customers"]["retrievePaymentMethods"]>[0],
    ): Promise<{
      items: Array<{
        payment_method?: string | null;
        payment_method_id: string;
        recurring_enabled?: boolean | null;
        card?: {
          last4_digits?: string | null;
          card_network?: string | null;
          expiry_month?: string | null;
          expiry_year?: string | null;
        } | null;
      }>;
    }>;
  };
  payments: {
    retrieve(paymentId: Parameters<DodoPayments["payments"]["retrieve"]>[0]): Promise<DodoPayment>;
  };
  subscriptions: {
    cancelChangePlan(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["cancelChangePlan"]>[0],
      options?: { idempotencyKey?: string },
    ): Promise<unknown>;
    changePlan(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["changePlan"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["changePlan"]>[1],
      options?: { idempotencyKey?: string },
    ): Promise<unknown>;
    previewChangePlan(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["previewChangePlan"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["previewChangePlan"]>[1],
    ): Promise<DodoPlanChangePreview>;
    update(
      subscriptionId: Parameters<DodoPayments["subscriptions"]["update"]>[0],
      body: Parameters<DodoPayments["subscriptions"]["update"]>[1],
      options?: { idempotencyKey?: string },
    ): Promise<unknown>;
  };
}
