import Stripe from "stripe";
import type { BillingEventSink } from "../../bursar.js";
import type { BillingPaymentInfo, BillingSubscriptionStatus } from "../../billing/types/index.js";
import { type ProviderLogger, normalizeProviderLogger } from "../types.js";
import {
  callBillingEventSink,
  requireCurrency,
  requireMinorUnits,
  requireProviderString,
} from "../_shared.js";

const STRIPE_CHECKOUT_EXPAND = ["line_items"] as const;

function buildEnd(subscription: Stripe.Subscription): string | null {
  const raw = (subscription as { current_period_end?: number }).current_period_end;
  return raw ? new Date(raw * 1000).toISOString() : null;
}

function buildStart(subscription: Stripe.Subscription): string | null {
  const raw = (subscription as { current_period_start?: number }).current_period_start;
  return raw ? new Date(raw * 1000).toISOString() : null;
}

function timestamp(value: number | null | undefined): string | null {
  return value == null ? null : new Date(value * 1000).toISOString();
}

function subscriptionRefs(subscription: Stripe.Subscription) {
  const price = subscription.items?.data?.[0]?.price;
  if (!price) return undefined;
  return {
    priceId: price.id,
    productId: typeof price.product === "string" ? price.product : price.product?.id,
  };
}

function buildEndFromInvoice(invoice: Stripe.Invoice): string | null {
  return invoice.period_end ? new Date(invoice.period_end * 1000).toISOString() : null;
}

function buildStartFromInvoice(invoice: Stripe.Invoice): string | null {
  return invoice.period_start ? new Date(invoice.period_start * 1000).toISOString() : null;
}

function customerId(
  customer: string | Stripe.Customer | Stripe.DeletedCustomer | null,
): string | null {
  if (!customer) return null;
  return typeof customer === "string" ? customer : customer.id;
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
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        const userId = session.client_reference_id;
        if (!userId) {
          log.warn("Webhook: no client_reference_id", { sessionId: session.id });
          break;
        }

        let expandedSession: Stripe.Checkout.Session;
        try {
          expandedSession = await stripe.checkout.sessions.retrieve(session.id, {
            expand: [...STRIPE_CHECKOUT_EXPAND],
          });
        } catch (err) {
          log.error("Failed to retrieve expanded session", { sessionId: session.id, err });
          break;
        }

        const customer =
          typeof session.customer === "string"
            ? null
            : (session.customer as Stripe.Customer | null);
        const customerInfo = {
          providerCustomerId: customerId(session.customer),
          email: customer?.email ?? null,
        };

        if (session.mode === "subscription" && session.subscription) {
          const subId =
            typeof session.subscription === "string"
              ? session.subscription
              : session.subscription.id;
          try {
            const sub = await stripe.subscriptions.retrieve(subId);
            const end = buildEnd(sub);
            const currentPeriodStart = buildStart(sub);
            const planSlug = session.metadata?.plan_slug as string | undefined;

            await callBillingEventSink(sink, {
              provider: "stripe",
              eventId: event.id,
              eventType: "checkout.completed",
              occurredAt,
              userId,
              customer: customerInfo,
              subscription: {
                providerSubscriptionId: subId,
                status: sub.status as BillingSubscriptionStatus,
                cancelAtPeriodEnd: sub.cancel_at_period_end,
                periodEnd: end,
                periodStart: currentPeriodStart,
                trialEnd: timestamp(sub.trial_end),
                cancelAt: timestamp(sub.cancel_at),
                endedAt: timestamp(sub.ended_at),
                refs: planSlug ? { lookupKey: planSlug } : subscriptionRefs(sub),
              },
            });
          } catch (err) {
            log.error("Failed to process subscription", {
              userId,
              subscriptionId: subId,
              err,
            });
          }
        } else {
          const priceId = expandedSession.line_items?.data[0]?.price?.id;
          const productId = expandedSession.line_items?.data[0]?.price?.product;
          const providerPaymentId =
            typeof session.payment_intent === "string"
              ? session.payment_intent
              : session.payment_intent?.id;
          const payment: BillingPaymentInfo = {
            providerPaymentId: requireProviderString(
              providerPaymentId,
              "Stripe checkout session.payment_intent",
            ),
            amountMinor: requireMinorUnits(
              session.amount_total,
              "Stripe checkout session.amount_total",
            ),
            taxMinor: 0,
            currency: requireCurrency(session.currency, "Stripe checkout session.currency"),
            purpose: "credit_topup",
            status: "succeeded",
            refs: {
              productId: productId ? String(productId) : undefined,
              priceId: priceId ?? undefined,
            },
          };

          await callBillingEventSink(sink, {
            provider: "stripe",
            eventId: event.id,
            eventType: "payment.succeeded",
            occurredAt,
            userId,
            customer: customerInfo,
            payment,
          });
        }
        break;
      }

      case "customer.subscription.updated": {
        const sub = event.data.object as Stripe.Subscription;
        const userId = sub.metadata?.userId;
        if (!userId) {
          log.debug("customer.subscription.updated: no userId in metadata", {
            subscriptionId: sub.id,
          });
          break;
        }

        const end = buildEnd(sub);
        const eventType =
          sub.status === "canceled"
            ? "subscription.canceled"
            : sub.cancel_at_period_end
              ? "subscription.cancellation_scheduled"
              : "subscription.updated";

        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType,
          occurredAt,
          userId,
          customer: {
            providerCustomerId: customerId(sub.customer),
          },
          subscription: {
            providerSubscriptionId: sub.id,
            status: sub.status as BillingSubscriptionStatus,
            cancelAtPeriodEnd: sub.cancel_at_period_end,
            periodStart: buildStart(sub),
            periodEnd: end,
            trialEnd: timestamp(sub.trial_end),
            cancelAt: timestamp(sub.cancel_at),
            endedAt: timestamp(sub.ended_at),
            refs: subscriptionRefs(sub),
          },
        });
        break;
      }

      case "customer.subscription.deleted": {
        const sub = event.data.object as Stripe.Subscription;
        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: "subscription.canceled",
          occurredAt,
          customer: {
            providerCustomerId: customerId(sub.customer),
          },
          subscription: {
            providerSubscriptionId: sub.id,
            status: "canceled",
            periodStart: buildStart(sub),
            periodEnd: buildEnd(sub),
            trialEnd: timestamp(sub.trial_end),
            cancelAt: timestamp(sub.cancel_at),
            endedAt: timestamp(sub.ended_at) ?? occurredAt,
            refs: subscriptionRefs(sub),
          },
        });
        break;
      }

      case "payment_intent.succeeded":
      case "payment_intent.payment_failed": {
        const intent = event.data.object as Stripe.PaymentIntent;
        const metadata = intent.metadata ?? {};
        if (!metadata.auto_recharge_attempt_id) break;
        const succeeded = event.type === "payment_intent.succeeded";
        const payment: BillingPaymentInfo = {
          providerPaymentId: intent.id,
          amountMinor: requireMinorUnits(intent.amount, "Stripe payment intent.amount"),
          taxMinor: 0,
          currency: requireCurrency(intent.currency, "Stripe payment intent.currency"),
          purpose: "credit_topup",
          status: succeeded ? "succeeded" : "failed",
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
          userId: metadata.userId,
          payment,
        });
        break;
      }

      case "invoice.paid": {
        const invoice = event.data.object as Stripe.Invoice;
        const subscriptionId = (invoice as { subscription?: string }).subscription;
        if (!subscriptionId) {
          log.debug("invoice.paid: no subscription reference", { invoiceId: invoice.id });
          break;
        }

        let userId: string | undefined;

        if (invoice.metadata?.userId) {
          userId = invoice.metadata.userId;
        }

        if (!userId && invoice.parent?.subscription_details?.metadata) {
          userId = invoice.parent.subscription_details.metadata.userId;
        }

        let stripeSub: Stripe.Subscription | undefined;
        try {
          stripeSub = await stripe.subscriptions.retrieve(subscriptionId);
          if (!userId) {
            userId = stripeSub.metadata?.userId;
          }
        } catch (err) {
          if (!userId) {
            log.error("invoice.paid: failed to retrieve subscription", {
              subscriptionId,
              err,
            });
            break;
          }
        }
        if (!userId) {
          log.warn("invoice.paid: no userId", { subscriptionId });
          break;
        }

        const subStatus =
          stripeSub?.status ??
          (invoice.collection_method === "send_invoice" ? "active" : "incomplete");
        const periodEnd = stripeSub ? buildEnd(stripeSub) : buildEndFromInvoice(invoice);
        const periodStart = stripeSub ? buildStart(stripeSub) : buildStartFromInvoice(invoice);

        await callBillingEventSink(sink, {
          provider: "stripe",
          eventId: event.id,
          eventType: "invoice.paid",
          occurredAt,
          userId,
          customer: {
            providerCustomerId: customerId(invoice.customer),
          },
          subscription: {
            providerSubscriptionId: subscriptionId,
            status: subStatus as BillingSubscriptionStatus,
            periodEnd,
            periodStart,
            trialEnd: timestamp(stripeSub?.trial_end),
            cancelAt: timestamp(stripeSub?.cancel_at),
            endedAt: timestamp(stripeSub?.ended_at),
            refs: stripeSub ? subscriptionRefs(stripeSub) : undefined,
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
        });
        break;
      }

      case "refund.created":
      case "refund.updated":
      case "refund.failed": {
        const refund = event.data.object as Stripe.Refund;
        const status =
          refund.status === "succeeded" ||
          refund.status === "failed" ||
          refund.status === "canceled"
            ? refund.status
            : "pending";
        const providerPaymentId =
          typeof refund.payment_intent === "string"
            ? refund.payment_intent
            : refund.payment_intent?.id;
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
          userId: refund.metadata?.userId,
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
      err,
    });
    throw err;
  }

  return { received: true };
}
