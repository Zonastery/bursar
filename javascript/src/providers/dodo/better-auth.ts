import { webhooks as officialDodoWebhooks } from "@dodopayments/better-auth";

import type {
  BetterAuthBursarUser,
  BetterAuthProviderCustomer,
} from "../../integrations/better-auth.js";
import type { WebhookResult } from "../types.js";
import { createDodoWebhookBridge, type DodoWebhookBridgeOptions } from "./webhook-bridge.js";

type OfficialWebhookConfig = Parameters<typeof officialDodoWebhooks>[0];
type OfficialOnPayload = NonNullable<OfficialWebhookConfig["onPayload"]>;
export type DodoBetterAuthWebhookPayload = Parameters<OfficialOnPayload>[0];
export type DodoBetterAuthWebhookEventHandlers = Omit<
  OfficialWebhookConfig,
  "webhookKey" | "onPayload"
>;

export interface DodoBetterAuthWebhookOptions extends DodoWebhookBridgeOptions {
  webhookKey: string;
  eventHandlers?: DodoBetterAuthWebhookEventHandlers;
  onProcessed?: (
    result: WebhookResult,
    payload: DodoBetterAuthWebhookPayload,
  ) => void | Promise<void>;
}

/** Build a Better Auth Dodo webhook plugin backed by Bursar ingestion. */
export function dodoBetterAuthWebhooks(options: DodoBetterAuthWebhookOptions) {
  const { webhookKey, eventHandlers, onProcessed, ...bridgeOptions } = options;
  const processPayload = createDodoWebhookBridge(bridgeOptions);

  return officialDodoWebhooks({
    ...eventHandlers,
    webhookKey,
    onPayload: async (payload) => {
      const result = await processPayload(payload);
      await onProcessed?.(result, payload);
    },
  });
}

/** Resolve the customer field maintained by `@dodopayments/better-auth`. */
export function dodoBetterAuthCustomer(
  user: BetterAuthBursarUser,
): BetterAuthProviderCustomer | null {
  const customerId = user.dodoCustomerId;
  return typeof customerId === "string" && customerId.trim()
    ? { provider: "dodo", customerId: customerId.trim() }
    : null;
}
