import type { WebhookResult } from "../types.js";
import { createDodoWebhookBridge, type DodoWebhookBridgeOptions } from "./webhook-bridge.js";

type OfficialWebhooks = typeof import("@dodopayments/nextjs").Webhooks;
type OfficialWebhookConfig = Parameters<OfficialWebhooks>[0];
type OfficialOnPayload = NonNullable<OfficialWebhookConfig["onPayload"]>;
export type DodoNextWebhookHandler = ReturnType<OfficialWebhooks>;
export type DodoNextWebhookPayload = Parameters<OfficialOnPayload>[0];

export interface DodoNextWebhookAdapterConfig {
  webhookKey: string;
  eventHandlers?: Omit<OfficialWebhookConfig, "webhookKey" | "onPayload">;
  onPayload(payload: DodoNextWebhookPayload): Promise<void>;
}

export type DodoNextWebhookAdapter = (
  config: DodoNextWebhookAdapterConfig,
) => DodoNextWebhookHandler;

export interface DodoNextWebhookCoreOptions extends DodoWebhookBridgeOptions {
  webhookKey: string;
  eventHandlers?: Omit<OfficialWebhookConfig, "webhookKey" | "onPayload">;
  onProcessed?: (result: WebhookResult, payload: DodoNextWebhookPayload) => void | Promise<void>;
  adapter: DodoNextWebhookAdapter;
}

export function createDodoNextWebhookHandlerCore(options: DodoNextWebhookCoreOptions) {
  const { webhookKey, eventHandlers, onProcessed, adapter, ...bridgeOptions } = options;
  const processPayload = createDodoWebhookBridge(bridgeOptions);
  return adapter({
    webhookKey,
    eventHandlers,
    onPayload: async (payload) => {
      const result = await processPayload(payload);
      await onProcessed?.(result, payload);
    },
  });
}
