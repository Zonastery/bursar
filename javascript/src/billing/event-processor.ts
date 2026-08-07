import type { NormalizedLogger } from "../shared/logger.js";
import { normalizeLogger } from "../shared/logger.js";
import type { BillingStore } from "./billing-store.js";
import type { BillingEvent, BillingEventHandler, BillingEventResult } from "./types/index.js";
import { assertBillingEvent, BillingEventType } from "./types/index.js";
import { BillingEventHandlers } from "./event-handlers.js";
import type { BillingServiceOptions } from "./service-types.js";
import { boundedDiagnosticMessage } from "../shared/diagnostics.js";

export class BillingEventProcessor {
  private readonly eventHandlers: Partial<Record<BillingEventType, BillingEventHandler>>;
  private readonly logger: NormalizedLogger;
  private readonly handlers: BillingEventHandlers;
  private readonly IGNORED_EVENT_TYPES: Set<BillingEventType> = new Set([
    BillingEventType.CHECKOUT_EXPIRED,
    BillingEventType.INVOICE_UPCOMING,
  ]);

  get hasProvisioning(): boolean {
    return this.handlers.hasProvisioning;
  }

  constructor(
    private readonly store: BillingStore,
    options?: BillingServiceOptions,
  ) {
    this.eventHandlers = options?.eventHandlers ?? {};
    this.logger = normalizeLogger(options?.logger);
    this.handlers = new BillingEventHandlers(store, options);
  }

  /** Invalidate the offer cache so the next resolution call re-fetches fresh data. */
  invalidateOfferCache(): void {
    this.handlers.invalidateOfferCache();
  }

  async ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult> {
    assertBillingEvent(event);
    this.logger.debug("[BillingService] ingestBillingEvent", {
      eventId: event.eventId,
      provider: event.provider,
      eventType: event.eventType,
    });
    const claimEnvelope = { ...event } as Record<string, unknown>;
    delete claimEnvelope["occurredAt"];
    delete claimEnvelope["raw"];
    delete claimEnvelope["billingEventId"];
    const claim = await this.store.claimBillingEvent(
      event.provider,
      event.eventId,
      event.eventType,
      claimEnvelope,
    );
    this.logger.debug("[BillingService] claim status", {
      status: claim.status,
      eventId: event.eventId,
    });

    if (claim.status === "duplicate") {
      this.logger.debug("[BillingService] duplicate event", { eventId: event.eventId });
      return { handled: true, action: "duplicate" };
    }
    if (claim.status === "busy") {
      this.logger.debug("[BillingService] event is already processing", { eventId: event.eventId });
      return { handled: false, error: "claim_busy" };
    }
    if (claim.status === "retry") {
      this.logger.warn("[BillingService] claim retry", { eventId: event.eventId });
      return { handled: false, error: "claim_failed_retry" };
    }

    let result: BillingEventResult;
    try {
      result = await this.routeEvent({
        ...event,
        billingEventId: claim.billingEventId,
      });
      this.logger.debug("[BillingService] routeEvent result", { result, eventId: event.eventId });
    } catch (err) {
      return this.recordBillingEventFailure(event, claim.claimToken, err);
    }

    if (!result.handled) {
      const message = boundedDiagnosticMessage(result.error, "billing_event_not_handled");
      await this.recordBillingEventFailure(event, claim.claimToken, message, false);
      return { ...result, error: message };
    }

    let completed: boolean;
    try {
      completed = await this.store.completeBillingEvent(
        event.provider,
        event.eventId,
        claim.claimToken,
      );
    } catch (err) {
      return this.recordBillingEventFailure(event, claim.claimToken, err);
    }

    if (!completed) {
      return this.recordBillingEventFailure(
        event,
        claim.claimToken,
        "billing_event_completion_rejected",
      );
    }
    return result;
  }

  private async recordBillingEventFailure(
    event: BillingEvent,
    claimToken: string,
    error: unknown,
    logAsError = true,
  ): Promise<BillingEventResult> {
    const message = boundedDiagnosticMessage(error, "billing_event_processing_failed");
    const log = logAsError ? this.logger.error : this.logger.warn;
    log(`[BillingService] failed to handle billing event ${event.provider}/${event.eventId}`, {
      error: message,
    });
    const failed = await this.store.failBillingEvent(
      event.provider,
      event.eventId,
      claimToken,
      message,
    );
    if (!failed) {
      this.logger.warn("[BillingService] billing event failure was not persisted", {
        provider: event.provider,
        eventId: event.eventId,
      });
    }
    return { handled: false, error: message };
  }

  private async routeEvent(event: BillingEvent): Promise<BillingEventResult> {
    const handler = this.handlers.getHandler(event.eventType);
    if (!handler) {
      if (this.IGNORED_EVENT_TYPES.has(event.eventType)) {
        if (event.eventType === BillingEventType.CHECKOUT_EXPIRED) {
          await this.handlers.updateCheckoutIntentFromEvent(event, "expired");
        }
        return { handled: true, action: "ignored" };
      }
      return { handled: false, error: "unhandled_event_type" };
    }
    const result = await handler(event);
    if (result.handled) {
      await this.fireEventHandlers(event, event.userId ?? null);
    }
    return result;
  }

  private async fireEventHandlers(event: BillingEvent, userId: string | null): Promise<void> {
    if (!userId) return;
    const handler = this.eventHandlers[event.eventType];
    if (!handler) return;
    try {
      await handler(event, userId);
    } catch (err) {
      this.logger.error(
        `[BillingService] event handler failed for ${event.provider}/${event.eventId}`,
        { error: err instanceof Error ? err.message : String(err) },
      );
    }
  }

  async revokeIfCurrentSubscription(userId: string, subscriptionId: string): Promise<void> {
    await this.handlers.revokeIfCurrentSubscription(userId, subscriptionId);
  }
}
