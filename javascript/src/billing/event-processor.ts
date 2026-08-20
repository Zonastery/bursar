import { z } from "zod";

import { boundedDiagnosticMessage, persistedDiagnosticSummary } from "../shared/diagnostics.js";
import type { LogContext, NormalizedLogger } from "../shared/logger.js";
import { normalizeLogger } from "../shared/logger.js";
import type { BillingStore } from "./billing-store.js";
import type { BillingEvent, BillingEventHandler, BillingEventResult } from "./types/index.js";
import { assertBillingEvent, BillingEventType } from "./types/index.js";
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

  get terminalPlanKey(): string | null {
    return this.handlers.terminalPlan;
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
    const claimEnvelope = Object.fromEntries(
      Object.entries(event).filter(
        ([key, value]) =>
          key !== "occurredAt" && key !== "raw" && key !== "billingEventId" && value !== undefined,
      ),
    );
    const claimDocument = z.record(z.string(), z.json()).parse(claimEnvelope);
    const claim = await this.store.claimBillingEvent(
      event.provider,
      event.eventId,
      event.eventType,
      claimDocument,
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
    if (
      claim.status === "invalid_request" ||
      claim.status === "idempotency_conflict" ||
      claim.status === "max_retries_exceeded"
    ) {
      const rejectionContext: LogContext = {
        status: claim.status,
        eventId: event.eventId,
      };
      if (claim.status !== "invalid_request") {
        rejectionContext.billingEventId = claim.billingEventId;
      }
      this.logger.warn("[BillingService] permanent claim rejection", rejectionContext);
      return { handled: false, error: claim.status };
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
      this.logger.debug("[BillingService] routeEvent result", {
        handled: result.handled,
        action: result.action ?? null,
        error: result.error ?? null,
        eventId: event.eventId,
      });
    } catch (cause: unknown) {
      return this.recordBillingEventFailure(event, claim.claimToken, cause);
    }

    if (!result.handled) {
      const message = boundedDiagnosticMessage(result.error, "billing_event_not_handled");
      await this.recordBillingEventFailure(event, claim.claimToken, message, false, true);
      return { ...result, error: message };
    }

    let completed: boolean;
    try {
      completed = await this.store.completeBillingEvent(
        event.provider,
        event.eventId,
        claim.claimToken,
      );
    } catch (cause: unknown) {
      return this.recordBillingEventFailure(event, claim.claimToken, cause);
    }

    if (!completed) {
      return this.recordBillingEventFailure(
        event,
        claim.claimToken,
        "billing_event_completion_rejected",
        true,
        true,
      );
    }
    return result;
  }

  private async recordBillingEventFailure(
    event: BillingEvent,
    claimToken: string,
    cause: unknown,
    logAsError = true,
    trustedCode = false,
  ): Promise<BillingEventResult> {
    const message = trustedCode
      ? boundedDiagnosticMessage(cause, "billing_event_processing_failed")
      : persistedDiagnosticSummary(cause, "billing_event_processing_failed");
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
    if (result.handled && result.action !== "stale_subscription_event") {
      await this.fireEventHandlers(event, event.accountId ?? null);
    }
    return result;
  }

  private async fireEventHandlers(event: BillingEvent, accountId: string | null): Promise<void> {
    if (!accountId) return;
    const handler = this.eventHandlers[event.eventType];
    if (!handler) return;
    try {
      await handler(event, accountId);
    } catch (cause: unknown) {
      this.logger.error(
        `[BillingService] event handler failed for ${event.provider}/${event.eventId}`,
        {
          error: persistedDiagnosticSummary(cause, "billing_event_callback_failed"),
        },
      );
    }
  }
}
