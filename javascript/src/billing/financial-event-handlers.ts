import type { NormalizedLogger } from "../shared/logger.js";
import type { BillingStore } from "./billing-store.js";
import type {
  BillingEvent,
  BillingEventResult,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  BillingTopupResult,
} from "./types/index.js";

export interface FinancialSubscriptionOverrides {
  status?: BillingSubscriptionStatus | null;
  graceEndsAt?: string | null;
  graceExpiredAt?: string | null;
}

export class BillingFinancialEventHandlers {
  constructor(
    private readonly store: BillingStore,
    private readonly logger: NormalizedLogger,
    private readonly pastDueGracePeriodMs: number,
    private readonly resolveUserId: (event: BillingEvent) => Promise<string | null>,
    private readonly handleSubscriptionRenewed: (
      event: BillingEvent,
    ) => Promise<BillingEventResult>,
    private readonly updateCheckoutIntentFromEvent: (
      event: BillingEvent,
      status: "completed" | "failed" | "expired",
    ) => Promise<void>,
    private readonly getExistingSubscription: (
      event: BillingEvent,
    ) => Promise<BillingSubscriptionState | null>,
    private readonly buildSubscriptionState: (
      event: BillingEvent,
      userId: string,
      existing: BillingSubscriptionState | null,
      overrides?: FinancialSubscriptionOverrides,
    ) => BillingSubscriptionState,
  ) {}

  async handleInvoicePaid(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleInvoicePaid", {
      provider: event.provider,
      eventId: event.eventId,
      invoiceId: event.invoice?.providerInvoiceId,
    });
    const renewalResult = event.subscription
      ? await this.handleSubscriptionRenewed(event)
      : { handled: true, action: "invoice_paid" };
    if (!renewalResult.handled) return renewalResult;
    if (event.invoice) {
      const uid = await this.resolveUserId(event);
      if (uid) {
        await this.store.upsertBillingInvoice({
          provider: event.provider,
          providerInvoiceId: event.invoice.providerInvoiceId,
          providerSubscriptionId: event.subscription?.providerSubscriptionId,
          userId: uid,
          status: event.invoice.status,
          amountPaidMinor: event.invoice.amountPaidMinor,
          amountDueMinor: event.invoice.amountDueMinor,
          currency: event.invoice.currency,
          periodStart: event.invoice.periodStart,
          periodEnd: event.invoice.periodEnd,
          metadata: event.metadata,
          providerUpdatedAt: event.occurredAt,
        });
      }
    }
    return renewalResult;
  }

  async handlePaymentSucceeded(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handlePaymentSucceeded", {
      provider: event.provider,
      eventId: event.eventId,
      amountMinor: event.payment?.amountMinor,
    });
    if (!event.payment) return { handled: true, action: "payment_succeeded" };

    const uid = await this.resolveUserId(event);
    const refs = event.payment.refs;
    let topupConfig: BillingTopupResult | null = null;
    if (refs) {
      topupConfig = await this.store.resolveCreditTopup(
        event.provider,
        refs.productId ?? null,
        refs.priceId ?? null,
      );
    }

    let paymentId: string | null = null;
    if (uid) {
      const paymentMetadata: Record<string, unknown> | null =
        topupConfig && event.payment.purpose === "credit_topup"
          ? { credits_per_unit: Number(topupConfig.creditsPerUnit ?? 1000) }
          : null;
      paymentId = await this.store.upsertBillingPayment({
        provider: event.provider,
        providerPaymentId: event.payment.providerPaymentId,
        userId: uid,
        amountMinor: event.payment.amountMinor,
        taxMinor: event.payment.taxMinor,
        currency: event.payment.currency,
        purpose: event.payment.purpose,
        status: event.payment.status ?? "succeeded",
        providerUpdatedAt: event.occurredAt,
        metadata: paymentMetadata,
      });

      // Dodo represents subscription receipts as payment.succeeded events,
      // while Stripe emits invoice.paid. Materialize the Dodo payment as a
      // paid invoice so invoice history is provider-agnostic.
      if (event.payment.purpose === "subscription" && event.subscription) {
        const renewalResult = await this.handleSubscriptionRenewed(event);
        if (!renewalResult.handled) return renewalResult;
        await this.store.upsertBillingInvoice({
          provider: event.provider,
          providerInvoiceId: event.payment.providerPaymentId,
          providerSubscriptionId: event.subscription.providerSubscriptionId,
          userId: uid,
          status: "paid",
          amountPaidMinor: event.payment.amountMinor,
          amountDueMinor: event.payment.amountMinor,
          currency: event.payment.currency,
          periodStart: event.subscription.periodStart,
          periodEnd: event.subscription.periodEnd,
          metadata: event.metadata,
          providerUpdatedAt: event.occurredAt,
        });
      }
    }

    if (topupConfig && event.payment.purpose === "credit_topup" && uid) {
      if (
        (topupConfig.minAmountMinor != null &&
          event.payment.amountMinor < topupConfig.minAmountMinor) ||
        (topupConfig.maxAmountMinor != null &&
          event.payment.amountMinor > topupConfig.maxAmountMinor)
      ) {
        this.logger.warn(
          `[BillingService] topup amount ${event.payment.amountMinor} exceeds cap ${topupConfig.maxAmountMinor} for topup key ${topupConfig.topupKey} (user ${uid})`,
        );
        return { handled: true, action: "payment_succeeded_out_of_bounds" };
      }
      const credits = await this.store.computeTopupCredits(event.payment.amountMinor, topupConfig);
      const quantity =
        topupConfig.amountMinor == null || topupConfig.amountMinor <= 0
          ? 0
          : event.payment.amountMinor / topupConfig.amountMinor;
      if (
        credits > 0 &&
        paymentId &&
        Number.isSafeInteger(quantity) &&
        (topupConfig.creditsPerUnit ?? 0) > 0
      ) {
        const grantId = await this.store.createBillingCreditGrant({
          paymentId,
          topupId: topupConfig.topupId,
          configuredCredits: topupConfig.creditsPerUnit!,
          quantity,
        });
        await this.store.grantBillingCredit(grantId, `billing:${event.eventId}:topup`);
      }
      await this.store.updateAutoRechargeAttemptByProviderPayment({
        provider: event.provider,
        providerPaymentId: event.payment.providerPaymentId,
        state: "succeeded",
      });
    }

    await this.updateCheckoutIntentFromEvent(event, "completed");

    return { handled: true, action: "payment_succeeded" };
  }

  async handlePaymentFailed(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handlePaymentFailed", {
      provider: event.provider,
      eventId: event.eventId,
    });
    const uid = await this.resolveUserId(event);
    if (uid && event.payment) {
      await this.store.upsertBillingPayment({
        provider: event.provider,
        providerPaymentId: event.payment.providerPaymentId,
        userId: uid,
        amountMinor: event.payment.amountMinor,
        currency: event.payment.currency,
        purpose: event.payment.purpose,
        status: event.payment.status ?? "failed",
        providerUpdatedAt: event.occurredAt,
      });
    }
    if (uid && event.subscription) {
      const existing = await this.getExistingSubscription(event);
      const occurredAt = new Date(event.occurredAt);
      const graceBase = Number.isNaN(occurredAt.getTime()) ? new Date() : occurredAt;
      const pastDue = this.buildSubscriptionState(event, uid, existing, {
        status: "past_due",
        graceEndsAt: new Date(graceBase.getTime() + this.pastDueGracePeriodMs).toISOString(),
        graceExpiredAt: null,
      });
      await this.store.upsertBillingSubscription(pastDue);
    }
    if (event.payment) {
      await this.store.updateAutoRechargeAttemptByProviderPayment({
        provider: event.provider,
        providerPaymentId: event.payment.providerPaymentId,
        state: "failed",
        failureCode: "provider_payment_failed",
      });
    }
    await this.updateCheckoutIntentFromEvent(event, "failed");
    return { handled: true, action: "payment_failed_recorded" };
  }

  async handleRefundCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (uid && event.refund) {
      const refundId = await this.store.upsertBillingRefund({
        provider: event.provider,
        providerRefundId: event.refund.providerRefundId,
        providerPaymentId: event.refund.providerPaymentId,
        userId: uid,
        amountMinor: event.refund.amountMinor,
        currency: event.refund.currency,
        reason: event.refund.reason,
        status: event.refund.status ?? "pending",
        providerUpdatedAt: event.occurredAt,
        metadata: event.metadata,
      });
      if ((event.refund.status ?? "pending") === "succeeded" && event.refund.providerPaymentId) {
        const payment = await this.store.getBillingPayment(
          event.provider,
          event.refund.providerPaymentId,
        );

        if (payment?.purpose === "credit_topup" && payment.id) {
          const grantId = await this.store.getBillingCreditGrantByPayment(String(payment.id));
          if (grantId) {
            const result = await this.store.postBillingRefund(
              refundId,
              grantId,
              event.refund.amountMinor,
              `billing:${event.eventId}:refund`,
            );
            if (!result.error_code) return { handled: true, action: "refund_clawback" };
          }
        }
      }
    }
    return { handled: true, action: "refund_recorded" };
  }

  async handleDisputeCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (uid && event.dispute) {
      await this.store.upsertBillingDispute({
        provider: event.provider,
        providerDisputeId: event.dispute.providerDisputeId,
        providerPaymentId: event.dispute.providerPaymentId,
        userId: uid,
        status: "needs_response",
        reason: event.dispute.reason,
        metadata: event.metadata,
        providerUpdatedAt: event.occurredAt,
      });
    }
    return { handled: true, action: "dispute_recorded" };
  }

  async handleDisputeClosed(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (uid && event.dispute) {
      await this.store.upsertBillingDispute({
        provider: event.provider,
        providerDisputeId: event.dispute.providerDisputeId,
        providerPaymentId: event.dispute.providerPaymentId,
        userId: uid,
        status: "closed",
        reason: event.dispute.reason,
        metadata: event.metadata,
        providerUpdatedAt: event.occurredAt,
      });
    }
    return { handled: true, action: "dispute_closed" };
  }
}
