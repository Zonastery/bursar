import { Webhooks } from "@dodopayments/nextjs";

import type { WebhookResult } from "../types.js";
import { createDodoWebhookBridge, type DodoWebhookBridgeOptions } from "./webhook-bridge.js";

type OfficialWebhookConfig = Parameters<typeof Webhooks>[0];
type OfficialOnPayload = NonNullable<OfficialWebhookConfig["onPayload"]>;
export type DodoNextWebhookPayload = Parameters<OfficialOnPayload>[0];
export type DodoNextWebhookEventHandlers = Omit<OfficialWebhookConfig, "webhookKey" | "onPayload">;

export interface DodoNextWebhookOptions extends DodoWebhookBridgeOptions {
  webhookKey: string;
  eventHandlers?: DodoNextWebhookEventHandlers;
  onProcessed?: (result: WebhookResult, payload: DodoNextWebhookPayload) => void | Promise<void>;
}

/** Build a Next.js App Router handler on top of Dodo's official adapter. */
export function createDodoNextWebhookHandler(options: DodoNextWebhookOptions) {
  const { webhookKey, eventHandlers, onProcessed, ...bridgeOptions } = options;
  const processPayload = createDodoWebhookBridge(bridgeOptions);

  return Webhooks({
    ...eventHandlers,
    webhookKey,
    onPayload: async (payload) => {
      const result = await processPayload(payload);
      await onProcessed?.(result, payload);
    },
  });
}
