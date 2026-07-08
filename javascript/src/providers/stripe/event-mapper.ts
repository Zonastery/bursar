import Stripe from "stripe";
import type { BillingManager } from "../../billing/index.js";
import type { BillingPaymentInfo } from "../../billing/billing-types.js";
import type { ProviderLogger } from "../types.js";

const STRIPE_CHECKOUT_EXPAND = ["line_items"] as const;

function buildEnd(subscription: Stripe.Subscription): string | null {
  const raw = (subscription as { current_period_end?: number }).current_period_end;
  return raw ? new Date(raw * 1000).toISOString() : null;
}

export async function handleStripeWebhook(
  event: Stripe.Event,
  bm: BillingManager,
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
          providerCustomerId:
            typeof session.customer === "string" ? session.customer : customer?.id,
          email: customer?.email ?? null,
        };

        await bm.handleEvent({
          provider: "stripe",
          eventId: `${event.id}_checkout`,
          eventType: "checkout.completed",
          occurredAt: new Date().toISOString(),
          userId,
          customer: customerInfo,
        });

        if (session.mode === "subscription" && session.subscription) {
          const subId =
            typeof session.subscription === "string"
              ? session.subscription
              : session.subscription.id;
          try {
            const sub = await stripe.subscriptions.retrieve(subId);
            const end = buildEnd(sub);
            const planSlug = session.metadata?.plan_slug as string | undefined;

            await bm.handleEvent({
              provider: "stripe",
              eventId: `${event.id}_sub`,
              eventType: "subscription.created",
              occurredAt: new Date().toISOString(),
              userId,
              customer: customerInfo,
              subscription: {
                providerSubscriptionId: subId,
                status: sub.status as
                  | "active"
                  | "trialing"
                  | "incomplete"
                  | "past_due"
                  | "canceled"
                  | "unpaid"
                  | "paused"
                  | "expired"
                  | "incomplete_expired",
                cancelAtPeriodEnd: sub.cancel_at_period_end,
                periodEnd: end,
                refs: planSlug ? { lookupKey: planSlug } : undefined,
              },
            });

            if (sub.status === "active" || sub.status === "trialing") {
              await bm.handleEvent({
                provider: "stripe",
                eventId: `${event.id}_sub_activated`,
                eventType: "subscription.activated",
                occurredAt: new Date().toISOString(),
                userId,
                customer: customerInfo,
                subscription: {
                  providerSubscriptionId: subId,
                  status: sub.status as "active" | "trialing",
                  periodEnd: end,
                },
              });
            }
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

          await bm.handleEvent({
            provider: "stripe",
            eventId: `${event.id}_payment`,
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

        await bm.handleEvent({
          provider: "stripe",
          eventId: event.id,
          eventType,
          occurredAt: new Date().toISOString(),
          userId,
          customer: {
            providerCustomerId: typeof sub.customer === "string" ? sub.customer : sub.customer.id,
          },
          subscription: {
            providerSubscriptionId: sub.id,
            status: sub.status as
              | "active"
              | "trialing"
              | "incomplete"
              | "past_due"
              | "canceled"
              | "unpaid"
              | "paused"
              | "expired"
              | "incomplete_expired",
            cancelAtPeriodEnd: sub.cancel_at_period_end,
            periodEnd: end,
          },
        });
        break;
      }

      case "customer.subscription.deleted": {
        const sub = event.data.object as Stripe.Subscription;
        await bm.handleEvent({
          provider: "stripe",
          eventId: event.id,
          eventType: "subscription.canceled",
          occurredAt: new Date().toISOString(),
          customer: {
            providerCustomerId: typeof sub.customer === "string" ? sub.customer : sub.customer.id,
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

        let stripeSub: Stripe.Subscription;
        try {
          stripeSub = await stripe.subscriptions.retrieve(subscriptionId);
        } catch (err) {
          logger?.error?.("invoice.paid: failed to retrieve subscription", { subscriptionId, err });
          break;
        }

        const userId = stripeSub.metadata?.userId;
        if (!userId) {
          logger?.warn?.("invoice.paid: no userId in metadata", { subscriptionId });
          break;
        }

        await bm.handleEvent({
          provider: "stripe",
          eventId: event.id,
          eventType: "invoice.paid",
          occurredAt: new Date().toISOString(),
          userId,
          customer: {
            providerCustomerId:
              typeof stripeSub.customer === "string" ? stripeSub.customer : stripeSub.customer.id,
          },
          subscription: {
            providerSubscriptionId: subscriptionId,
            status: stripeSub.status as
              | "active"
              | "trialing"
              | "incomplete"
              | "past_due"
              | "canceled"
              | "unpaid"
              | "paused"
              | "expired"
              | "incomplete_expired",
            periodEnd: buildEnd(stripeSub),
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
