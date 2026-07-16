import Stripe from "stripe";
import type { BillingEventSink } from "../../bursar.js";
import type { BillingPaymentInfo, BillingSubscriptionStatus } from "../../billing/billing-types.js";
import type { ProviderLogger } from "../types.js";
import { callBillingEventSink } from "../_shared.js";

const STRIPE_CHECKOUT_EXPAND = ["line_items"] as const;

function buildEnd(subscription: Stripe.Subscription): string | null {
  const raw = (subscription as { current_period_end?: number }).current_period_end;
  return raw ? new Date(raw * 1000).toISOString() : null;
}

function buildStart(subscription: Stripe.Subscription): string | null {
  const raw = (subscription as { current_period_start?: number }).current_period_start;
  return raw ? new Date(raw * 1000).toISOString() : null;
}

function buildEndFromInvoice(invoice: Stripe.Invoice): string | null {
  return invoice.period_end
    ? new Date(invoice.period_end * 1000).toISOString()
    : new Date().toISOString();
}

function buildStartFromInvoice(invoice: Stripe.Invoice): string | null {
  return invoice.period_start
    ? new Date(invoice.period_start * 1000).toISOString()
    : new Date().toISOString();
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
  logger?: ProviderLogger,
): Promise<{ received: boolean }> {
  try {
    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        const userId = session.client_reference_id;
        if (!userId) {
          logger?.warn?.("Webhook: no client_reference_id", { sessionId: session.id });
          break;
        }

        let expandedSession: Stripe.Checkout.Session;
        try {
          expandedSession = await stripe.checkout.sessions.retrieve(session.id, {
            expand: [...STRIPE_CHECKOUT_EXPAND],
          });
        } catch (err) {
          logger?.error?.("Failed to retrieve expanded session", { sessionId: session.id, err });
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
              occurredAt: new Date().toISOString(),
              userId,
              customer: customerInfo,
              subscription: {
                providerSubscriptionId: subId,
                status: sub.status as BillingSubscriptionStatus,
                cancelAtPeriodEnd: sub.cancel_at_period_end,
                periodEnd: end,
                periodStart: currentPeriodStart,
                refs: planSlug ? { lookupKey: planSlug } : undefined,
              },
            });
          } catch (err) {
            logger?.error?.("Failed to process subscription", {
              userId,
              subscriptionId: subId,
              err,
            });
          }
        } else {
          const priceId = expandedSession.line_items?.data[0]?.price?.id;
          const productId = expandedSession.line_items?.data[0]?.price?.product;
          const payment: BillingPaymentInfo = {
            providerPaymentId: String(session.payment_intent || session.id),
            amountMinor: session.amount_total ?? 0,
            taxMinor: null,
            currency: (session.currency ?? "usd").toUpperCase(),
            purpose: "credit_topup",
            refs: {
              productId: productId ? String(productId) : undefined,
              priceId: priceId ?? undefined,
            },
          };

          await callBillingEventSink(sink, {
            provider: "stripe",
            eventId: event.id,
            eventType: "payment.succeeded",
            occurredAt: new Date().toISOString(),
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
          logger?.debug?.("customer.subscription.updated: no userId in metadata", {
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
          occurredAt: new Date().toISOString(),
          userId,
          customer: {
            providerCustomerId: customerId(sub.customer),
          },
          subscription: {
            providerSubscriptionId: sub.id,
            status: sub.status as BillingSubscriptionStatus,
            cancelAtPeriodEnd: sub.cancel_at_period_end,
            periodEnd: end,
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
          occurredAt: new Date().toISOString(),
          customer: {
            providerCustomerId: customerId(sub.customer),
          },
          subscription: { providerSubscriptionId: sub.id },
        });
        break;
      }

      case "invoice.paid": {
        const invoice = event.data.object as Stripe.Invoice;
        const subscriptionId = (invoice as { subscription?: string }).subscription;
        if (!subscriptionId) {
          logger?.debug?.("invoice.paid: no subscription reference", { invoiceId: invoice.id });
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
        if (!userId) {
          try {
            stripeSub = await stripe.subscriptions.retrieve(subscriptionId);
            userId = stripeSub.metadata?.userId;
          } catch (err) {
            logger?.error?.("invoice.paid: failed to retrieve subscription", {
              subscriptionId,
              err,
            });
            break;
          }
        }
        if (!userId) {
          logger?.warn?.("invoice.paid: no userId", { subscriptionId });
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
          occurredAt: new Date().toISOString(),
          userId,
          customer: {
            providerCustomerId: customerId(invoice.customer),
          },
          subscription: {
            providerSubscriptionId: subscriptionId,
            status: subStatus as BillingSubscriptionStatus,
            periodEnd,
            periodStart,
          },
          invoice: {
            providerInvoiceId: invoice.id,
            status: invoice.status ?? "open",
            amountPaidMinor: invoice.amount_paid,
            amountDueMinor: invoice.amount_due,
            currency: invoice.currency?.toUpperCase() ?? "USD",
          },
        });
        break;
      }

      default:
        logger?.debug?.("Unhandled Stripe webhook event", {
          eventType: event.type,
          eventId: event.id,
        });
    }
  } catch (err) {
    logger?.error?.("Stripe webhook processing failed", {
      eventId: event.id,
      eventType: event.type,
      err,
    });
    throw err;
  }

  return { received: true };
}
