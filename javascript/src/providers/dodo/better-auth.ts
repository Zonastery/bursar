import { webhooks as officialDodoWebhooks } from "@dodopayments/better-auth";
import { z } from "zod";

import type {
  BetterAuthBursarUser,
  BetterAuthProviderCustomer,
} from "../../integrations/better-auth.js";
import type { WebhookResult } from "../types.js";
import {
  dodoBetterAuthWebhooksCore,
  type DodoBetterAuthWebhookAdapter,
  type DodoBetterAuthWebhookAdapterConfig,
  type DodoBetterAuthWebhookPayload,
  type DodoBetterAuthWebhookPlugin,
} from "./better-auth-core.js";
import type { DodoWebhookBridgeOptions } from "./webhook-bridge.js";

export type {
  DodoBetterAuthWebhookAdapter,
  DodoBetterAuthWebhookAdapterConfig,
  DodoBetterAuthWebhookPayload,
  DodoBetterAuthWebhookPlugin,
};
export type DodoBetterAuthWebhookEventHandlers = NonNullable<
  DodoBetterAuthWebhookAdapterConfig["eventHandlers"]
>;

export interface DodoBetterAuthWebhookOptions extends DodoWebhookBridgeOptions {
  webhookKey: string;
  eventHandlers?: DodoBetterAuthWebhookEventHandlers;
  onProcessed?: (
    result: WebhookResult,
    payload: DodoBetterAuthWebhookPayload,
  ) => void | Promise<void>;
  adapter?: DodoBetterAuthWebhookAdapter;
}

/** Build a Better Auth Dodo webhook plugin backed by Bursar ingestion. */
export function dodoBetterAuthWebhooks(options: DodoBetterAuthWebhookOptions) {
  const { adapter, ...coreOptions } = options;
  const createPlugin: DodoBetterAuthWebhookAdapter =
    adapter ??
    ((config) => {
      return officialDodoWebhooks({
        ...config.eventHandlers,
        webhookKey: config.webhookKey,
        onPayload: config.onPayload,
      });
    });
  return dodoBetterAuthWebhooksCore({
    ...coreOptions,
    adapter: createPlugin,
  });
}

/** Resolve the customer field maintained by `@dodopayments/better-auth`. */
export function dodoBetterAuthCustomer(
  user: BetterAuthBursarUser,
): BetterAuthProviderCustomer | null {
  const customerId = user.dodoCustomerId;
  const parsed = z.string().trim().min(1).safeParse(customerId);
  return parsed.success ? { provider: "dodo", customerId: parsed.data } : null;
}
