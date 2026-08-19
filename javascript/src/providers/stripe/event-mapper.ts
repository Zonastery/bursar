import Stripe from "stripe";
import { z } from "zod";
import type { BillingEventSink } from "../../bursar.js";
import type {
  BillingCustomerInfo,
  BillingDisputeInfo,
  BillingPaymentInfo,
  ProviderRef,
  BillingSubscriptionInfo,
} from "../../billing/types/index.js";
import { type ProviderLogger, normalizeProviderLogger } from "../types.js";
import { persistedDiagnosticSummary } from "../../shared/diagnostics.js";
import {
  callBillingEventSink,
  requireCurrency,
  requireMinorUnits,
  requireProviderString,
} from "../_shared.js";

const STRIPE_CHECKOUT_EXPAND = ["line_items"] as const;

const STRIPE_SUBSCRIPTION_LIFECYCLE_EVENTS = {
  "customer.subscription.created": "subscription.created",
  "customer.subscription.paused": "subscription.paused",
  "customer.subscription.resumed": "subscription.resumed",
  "customer.subscription.trial_will_end": "subscription.trial_will_end",
} as const;

const billingSubscriptionStatusSchema = z.enum([
  "incomplete",
  "incomplete_expired",
  "trialing",
  "active",
  "past_due",
  "canceled",
  "unpaid",
  "paused",
  "expired",
]);

function timestamp(value: number | null | undefined): string | null {
  return value == null ? null : new Date(value * 1000).toISOString();
}

function subscriptionPeriodValue(
  subscription: Stripe.Subscription,
  field: "current_period_start" | "current_period_end",
): number | null {
  return subscription.items.data[0]?.[field] ?? null;
}

function buildEnd(subscription: Stripe.Subscription): string | null {
  return timestamp(subscriptionPeriodValue(subscription, "current_period_end"));
}

function buildStart(subscription: Stripe.Subscription): string | null {
  return timestamp(subscriptionPeriodValue(subscription, "current_period_start"));
}

function subscriptionRefs(subscription: Stripe.Subscription) {
  const price = subscription.items?.data?.[0]?.price;
  if (!price) return undefined;
  return {
    priceId: price.id,
    productId: expandableId(price.product),
  };
}

function subscriptionInfo(
  subscription: Stripe.Subscription,
  refs: ProviderRef | undefined = subscriptionRefs(subscription),
): BillingSubscriptionInfo {
  const status = billingSubscriptionStatusSchema.safeParse(subscription.status);
  if (!status.success) {
    throw new Error(`Stripe returned an unsupported subscription status '${subscription.status}'`);
  }
  return {
    providerSubscriptionId: subscription.id,
    status: status.data,
    cancelAtPeriodEnd: subscription.cancel_at_period_end,
    periodStart: buildStart(subscription),
    periodEnd: buildEnd(subscription),
    trialEnd: timestamp(subscription.trial_end),
    cancelAt: timestamp(subscription.cancel_at),
    endedAt: timestamp(subscription.ended_at),
    refs,
  };
}

function buildEndFromInvoice(invoice: Stripe.Invoice): string | null {
  return timestamp(invoice.period_end);
}

function buildStartFromInvoice(invoice: Stripe.Invoice): string | null {
  return timestamp(invoice.period_start);
}

function expandableId(value: string | { id: string } | null | undefined): string | null {
  const stringValue = z.string().min(1).safeParse(value);
  if (stringValue.success) return stringValue.data;
  const objectValue = z.object({ id: z.string().min(1) }).safeParse(value);
  return objectValue.success ? objectValue.data.id : null;
}

function customerId(
  customer: string | Stripe.Customer | Stripe.DeletedCustomer | null,
): string | null {
  return expandableId(customer);
}

function checkoutCustomer(session: Stripe.Checkout.Session): BillingCustomerInfo | undefined {
  const customer = z
    .object({
      deleted: z.boolean().optional(),
      email: z.string().nullable().optional(),
    })
    .safeParse(session.customer);
  const providerCustomerId = customerId(session.customer);
  let email = session.customer_details?.email ?? null;
  if (customer.success && customer.data.deleted !== true && customer.data.email != null) {
    email = customer.data.email;
  }
  return providerCustomerId || email ? { providerCustomerId, email } : undefined;
}

function checkoutMetadata(session: Stripe.Checkout.Session): Record<string, string> {
  return session.metadata ?? {};
}

function checkoutPaymentInfo(
  session: Stripe.Checkout.Session,
  expandedSession: Stripe.Checkout.Session,
  status: "succeeded" | "failed",
): BillingPaymentInfo {
  const lineItem = expandedSession.line_items?.data[0];
  const price = lineItem?.price;
  const productId = price?.product;
  const paymentIntentId = expandableId(session.payment_intent);
  const taxMinor = requireMinorUnits(
    session.total_details?.amount_tax ?? 0,
    "Stripe checkout session.total_details.amount_tax",
  );

  return {
    providerPaymentId: requireProviderString(
      paymentIntentId ?? session.id,
      "Stripe checkout session payment identifier",
    ),
    // BillingPaymentInfo represents the pre-tax amount; tax is recorded separately.
    amountMinor: requireMinorUnits(
      session.amount_subtotal ?? Math.max(0, (session.amount_total ?? 0) - taxMinor),
      "Stripe checkout session.amount_subtotal",
    ),
    taxMinor,
    currency: requireCurrency(session.currency, "Stripe checkout session.currency"),
    purpose: session.mode === "subscription" ? "subscription" : "credit_topup",
    status,
    refs: price
      ? {
          productId: productId ? (expandableId(productId) ?? undefined) : undefined,
          priceId: price.id,
        }
      : undefined,
  };
}

function invoiceSubscriptionId(invoice: Stripe.Invoice): string | null {
  return expandableId(invoice.parent?.subscription_details?.subscription);
}

function invoiceMetadata(invoice: Stripe.Invoice) {
  return {
    ...(invoice.parent?.subscription_details?.metadata ?? {}),
    ...(invoice.metadata ?? {}),
  };
}

function invoicePaymentId(invoice: Stripe.Invoice): string {
  for (const invoicePayment of invoice.payments?.data ?? []) {
    const paymentIntentId = expandableId(invoicePayment.payment.payment_intent);
    if (paymentIntentId) return paymentIntentId;
  }
  return invoice.id;
}

function invoiceTaxMinor(invoice: Stripe.Invoice): number {
  return requireMinorUnits(
    invoice.total_taxes?.reduce((total, item) => total + item.amount, 0) ?? 0,
    "Stripe invoice.total_taxes",
  );
}

function stripeDisputeStatus(status: Stripe.Dispute.Status): BillingDisputeInfo["status"] {
  switch (status) {
    case "needs_response":
    case "warning_needs_response":
      return "needs_response";
    case "under_review":
    case "warning_under_review":
      return "under_review";
    case "won":
    case "prevented":
      return "won";
    case "lost":
      return "lost";
    case "warning_closed":
      return "closed";
    default:
      throw new TypeError(`Unsupported Stripe dispute status: ${status}`);
  }
}

export async function handleStripeWebhook(
  event: Stripe.Event,
  sink: BillingEventSink,
  stripe: Stripe,
  logger?: ProviderLogger | null,
): Promise<{ received: boolean }> {
  const log = normalizeProviderLogger(logger);
  if (!Number.isSafeInteger(event.created) || event.created < 0) {
    throw new TypeError("Stripe event.created must be a non-negative Unix timestamp");
  }
  const occurredAt = new Date(event.created * 1000).toISOString();
  try {
    switch (event.type) {
      case "checkout.session.completed":
      case "checkout.session.async_payment_succeeded":
      case "checkout.session.async_payment_failed": {
        const session = event.data.object;
        const failed = event.type === "checkout.session.async_payment_failed";

        if (event.type === "checkout.session.completed" && session.payment_status === "unpaid") {
          log.debug("Stripe Checkout completed with a delayed payment", {
            sessionId: session.id,
          });
          break;
        }

        const expandedSession = await stripe.checkout.sessions.retrieve(session.id, {
          expand: [...STRIPE_CHECKOUT_EXPAND],
        });
        const metadata = checkoutMetadata(session);
        const accountId = session.client_reference_id ?? metadata.bursar_account_id;
        const customer = checkoutCustomer(session);

        if (failed) {
          let subscription: BillingSubscriptionInfo | undefined;
          const subscriptionId = expandableId(session.subscription);
          if (subscriptionId) {
            subscription = subscriptionInfo(await stripe.subscriptions.retrieve(subscriptionId));
          }
          await callBillingEventSink(sink, {
            provider: "stripe",
            eventId: event.id,
            eventType: "payment.failed",
            occurredAt,
            accountId,
            customer,
            subscription,
            payment: checkoutPaymentInfo(session, expandedSession, "failed"),
            metadata,
          });
          break;
        }

        const subscriptionId = expandableId(session.subscription);
        if (session.mode === "subscription" && subscriptionId) {
          const subscription = await stripe.subscriptions.retrieve(subscriptionId);
          const planSlug = metadata.plan_slug;
          await callBillingEventSink(sink, {
            provider: "stripe",
            eventId: event.id,
            eventType: "checkout.completed",
            occurredAt,
            accountId,
            customer,
            subscription: subscriptionInfo(
              subscription,
              planSlug ? { lookupKey: planSlug } : subscriptionRefs(subscription),
            ),
            metadata,
          });
        } else {
          await callBillingEventSink(sink, {
            provider: "stripe",
            eventId: event.id,
            eventType: "payment.succeeded",
            occurredAt,
            accountId,
            customer,
            payment: checkoutPaymentInfo(session, expandedSession, "succeeded"),
            metadata,
          });
        }
        break;
      }

      case "checkout.session.expired": {
        const session = event.data.object;
        const metadata = checkoutMetadata(session);
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: "checkout.expired",
          occurredAt,
          accountId: session.client_reference_id ?? metadata.bursar_account_id,
          customer: checkoutCustomer(session),
          metadata,
        });
        break;
      }

      case "customer.subscription.created":
      case "customer.subscription.paused":
      case "customer.subscription.resumed":
      case "customer.subscription.trial_will_end": {
        const subscription = event.data.object;
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: STRIPE_SUBSCRIPTION_LIFECYCLE_EVENTS[event.type],
          occurredAt,
          accountId: subscription.metadata?.bursar_account_id,
          customer: {
            providerCustomerId: customerId(subscription.customer),
          },
          subscription: subscriptionInfo(subscription),
          metadata: subscription.metadata,
        });
        break;
      }

      case "customer.subscription.updated": {
        const subscription = event.data.object;
        const eventType =
          subscription.status === "canceled"
            ? "subscription.canceled"
            : subscription.cancel_at_period_end
              ? "subscription.cancellation_scheduled"
              : "subscription.updated";

        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType,
          occurredAt,
          accountId: subscription.metadata?.bursar_account_id,
          customer: {
            providerCustomerId: customerId(subscription.customer),
          },
          subscription: subscriptionInfo(subscription),
          metadata: subscription.metadata,
        });
        break;
      }

      case "customer.subscription.deleted": {
        const subscription = event.data.object;
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: "subscription.canceled",
          occurredAt,
          accountId: subscription.metadata?.bursar_account_id,
          customer: {
            providerCustomerId: customerId(subscription.customer),
          },
          subscription: {
            ...subscriptionInfo(subscription),
            status: "canceled",
            endedAt: timestamp(subscription.ended_at) ?? occurredAt,
          },
          metadata: subscription.metadata,
        });
        break;
      }

      case "payment_intent.succeeded":
      case "payment_intent.payment_failed":
      case "payment_intent.canceled": {
        const intent = event.data.object;
        const metadata = intent.metadata ?? {};
        if (!metadata.auto_recharge_attempt_id) break;
        const succeeded = event.type === "payment_intent.succeeded";
        const canceled = event.type === "payment_intent.canceled";
        const payment: BillingPaymentInfo = {
          providerPaymentId: intent.id,
          amountMinor: requireMinorUnits(intent.amount, "Stripe payment intent.amount"),
          taxMinor: 0,
          currency: requireCurrency(intent.currency, "Stripe payment intent.currency"),
          purpose: "credit_topup",
          status: succeeded ? "succeeded" : canceled ? "canceled" : "failed",
          refs: {
            productId: metadata.product_id,
            priceId: metadata.price_id,
          },
        };
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: succeeded ? "payment.succeeded" : "payment.failed",
          occurredAt,
          accountId: metadata.bursar_account_id,
          payment,
          metadata,
        });
        break;
      }

      case "charge.dispute.created":
      case "charge.dispute.updated":
      case "charge.dispute.closed": {
        const dispute = event.data.object;
        const providerPaymentId =
          expandableId(dispute.payment_intent) ?? expandableId(dispute.charge);
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: event.type === "charge.dispute.closed" ? "dispute.closed" : "dispute.created",
          occurredAt,
          accountId: dispute.metadata?.bursar_account_id,
          dispute: {
            providerDisputeId: dispute.id,
            providerPaymentId: requireProviderString(
              providerPaymentId,
              "Stripe dispute payment identifier",
            ),
            status: stripeDisputeStatus(dispute.status),
            reason: dispute.reason,
          },
          metadata: dispute.metadata,
        });
        break;
      }

      case "invoice.paid": {
        const invoice = event.data.object;
        const subscriptionId = invoiceSubscriptionId(invoice);
        if (!subscriptionId) {
          log.debug("invoice.paid: no subscription reference", { invoiceId: invoice.id });
          break;
        }

        const metadata = invoiceMetadata(invoice);
        const subscription = await stripe.subscriptions.retrieve(subscriptionId);
        const periodEnd = buildEnd(subscription) ?? buildEndFromInvoice(invoice);
        const periodStart = buildStart(subscription) ?? buildStartFromInvoice(invoice);

        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: "invoice.paid",
          occurredAt,
          accountId: metadata.bursar_account_id ?? subscription.metadata?.bursar_account_id,
          customer: {
            providerCustomerId: customerId(invoice.customer),
          },
          subscription: {
            ...subscriptionInfo(subscription),
            periodEnd,
            periodStart,
          },
          invoice: {
            providerInvoiceId: invoice.id,
            status: "paid",
            amountPaidMinor: requireMinorUnits(invoice.amount_paid, "Stripe invoice.amount_paid"),
            amountDueMinor: requireMinorUnits(invoice.amount_due, "Stripe invoice.amount_due"),
            currency: requireCurrency(invoice.currency, "Stripe invoice.currency"),
            periodStart,
            periodEnd,
          },
          metadata,
        });
        break;
      }

      case "invoice.payment_failed": {
        const invoice = event.data.object;
        const subscriptionId = invoiceSubscriptionId(invoice);
        if (!subscriptionId) {
          log.debug("invoice.payment_failed: no subscription reference", { invoiceId: invoice.id });
          break;
        }
        const metadata = invoiceMetadata(invoice);
        const subscription = await stripe.subscriptions.retrieve(subscriptionId);
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: "payment.failed",
          occurredAt,
          accountId: metadata.bursar_account_id ?? subscription.metadata?.bursar_account_id,
          customer: {
            providerCustomerId: customerId(invoice.customer),
          },
          subscription: subscriptionInfo(subscription),
          payment: {
            providerPaymentId: invoicePaymentId(invoice),
            amountMinor: requireMinorUnits(invoice.subtotal, "Stripe invoice.subtotal"),
            taxMinor: invoiceTaxMinor(invoice),
            currency: requireCurrency(invoice.currency, "Stripe invoice.currency"),
            purpose: "subscription",
            status: "failed",
            refs: subscriptionRefs(subscription),
          },
          metadata,
        });
        break;
      }

      case "refund.created":
      case "refund.updated":
      case "refund.failed": {
        const refund = event.data.object;
        const status =
          refund.status === "succeeded" ||
          refund.status === "failed" ||
          refund.status === "canceled"
            ? refund.status
            : "pending";
        const providerPaymentId = expandableId(refund.payment_intent);
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType:
            event.type === "refund.failed"
              ? "refund.failed"
              : event.type === "refund.updated"
                ? "refund.updated"
                : "refund.created",
          occurredAt,
          accountId: refund.metadata?.bursar_account_id,
          refund: {
            providerRefundId: refund.id,
            providerPaymentId: requireProviderString(
              providerPaymentId,
              "Stripe refund.payment_intent",
            ),
            amountMinor: requireMinorUnits(refund.amount, "Stripe refund.amount", true),
            currency: requireCurrency(refund.currency, "Stripe refund.currency"),
            reason: refund.reason,
            status,
          },
          metadata: refund.metadata,
        });
        break;
      }

      default:
        log.debug("Unhandled Stripe webhook event", {
          eventType: event.type,
          eventId: event.id,
        });
    }
  } catch (err) {
    log.error("Stripe webhook processing failed", {
      eventId: event.id,
      eventType: event.type,
      err: persistedDiagnosticSummary(err, "webhook_processing_failed"),
    });
    throw err;
  }

  return { received: true };
}
