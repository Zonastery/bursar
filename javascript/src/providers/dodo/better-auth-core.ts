import type { WebhookResult } from "../types.js";
import { createDodoWebhookBridge, type DodoWebhookBridgeOptions } from "./webhook-bridge.js";

type OfficialDodoWebhooks = typeof import("@dodopayments/better-auth").webhooks;
type OfficialWebhookConfig = Parameters<OfficialDodoWebhooks>[0];
type OfficialOnPayload = NonNullable<OfficialWebhookConfig["onPayload"]>;
export type DodoBetterAuthWebhookPlugin = ReturnType<OfficialDodoWebhooks>;
export type DodoBetterAuthWebhookPayload = Parameters<OfficialOnPayload>[0];

export interface DodoBetterAuthWebhookAdapterConfig {
  webhookKey: string;
  eventHandlers?: Omit<OfficialWebhookConfig, "webhookKey" | "onPayload">;
  onPayload(payload: DodoBetterAuthWebhookPayload): Promise<void>;
}

export type DodoBetterAuthWebhookAdapter = (
  config: DodoBetterAuthWebhookAdapterConfig,
) => DodoBetterAuthWebhookPlugin;

export interface DodoBetterAuthWebhookCoreOptions extends DodoWebhookBridgeOptions {
  webhookKey: string;
  eventHandlers?: Omit<OfficialWebhookConfig, "webhookKey" | "onPayload">;
  onProcessed?: (
    result: WebhookResult,
    payload: DodoBetterAuthWebhookPayload,
  ) => void | Promise<void>;
  adapter: DodoBetterAuthWebhookAdapter;
}

export function dodoBetterAuthWebhooksCore(options: DodoBetterAuthWebhookCoreOptions) {
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
