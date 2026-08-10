import type { BillingEventSink } from "../../billing/contracts.js";
import type { ProviderLogger, WebhookResult } from "../types.js";
import type { DodoWebhookPayload } from "./client-contract.js";
import { DodoWebhookProcessor } from "./provider.js";

export interface DodoWebhookBridgeOptions {
  /** Resolve the current Bursar billing sink lazily for serverless runtimes. */
  getEventSink: () => BillingEventSink | Promise<BillingEventSink>;
  logger?: ProviderLogger | null;
}

export type DodoVerifiedWebhookHandler = (payload: DodoWebhookPayload) => Promise<WebhookResult>;

/**
 * Create a framework-neutral callback for official Dodo webhook adapters.
 * The adapter owns verification and validation; Bursar owns lifecycle mapping
 * and idempotent billing ingestion.
 */
export function createDodoWebhookBridge(
  options: DodoWebhookBridgeOptions,
): DodoVerifiedWebhookHandler {
  const lazySink: BillingEventSink = {
    async ingestBillingEvent(event) {
      const sink = await options.getEventSink();
      return sink.ingestBillingEvent(event);
    },
  };
  const processor = new DodoWebhookProcessor({
    eventSink: lazySink,
    logger: options.logger,
  });
  return (payload) => processor.handle(payload);
}
