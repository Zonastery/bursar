import type { NormalizedLogger } from "../shared/logger.js";
import { normalizeLogger } from "../shared/logger.js";
import type { BillingStore } from "./billing-store.js";
import type { BillingEvent, BillingEventHandler, BillingEventResult } from "./types/index.js";
import { BillingEventType } from "./types/index.js";
import { BillingEventHandlers } from "./event-handlers.js";
import type { BillingServiceOptions } from "./service-types.js";

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
    if (claim.status === "retry") {
      this.logger.warn("[BillingService] claim retry", { eventId: event.eventId });
      return { handled: false, error: "claim_failed_retry" };
    }

    try {
      const result = await this.routeEvent({
        ...event,
        billingEventId: claim.billingEventId,
      });
      this.logger.debug("[BillingService] routeEvent result", { result, eventId: event.eventId });
      await this.store.completeBillingEvent(event.provider, event.eventId, claim.claimToken);
      return result;
    } catch (err) {
      this.logger.error(
        `[BillingService] failed to handle billing event ${event.provider}/${event.eventId}`,
        { error: err instanceof Error ? err.message : String(err) },
      );
      await this.store.failBillingEvent(
        event.provider,
        event.eventId,
        claim.claimToken,
        err instanceof Error ? err.message : String(err),
      );
      return {
        handled: false,
        error: err instanceof Error ? err.message : String(err),
      };
    }
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
