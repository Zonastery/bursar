import { Webhooks } from "@dodopayments/nextjs";

import type { WebhookResult } from "../types.js";

import {
  createDodoNextWebhookHandlerCore,
  type DodoNextWebhookAdapter,
  type DodoNextWebhookAdapterConfig,
  type DodoNextWebhookHandler,
  type DodoNextWebhookPayload,
} from "./nextjs-core.js";
import type { DodoWebhookBridgeOptions } from "./webhook-bridge.js";

export type {
  DodoNextWebhookAdapter,
  DodoNextWebhookAdapterConfig,
  DodoNextWebhookHandler,
  DodoNextWebhookPayload,
};
export type DodoNextWebhookEventHandlers = NonNullable<
  DodoNextWebhookAdapterConfig["eventHandlers"]
>;

export interface DodoNextWebhookOptions extends DodoWebhookBridgeOptions {
  webhookKey: string;
  eventHandlers?: DodoNextWebhookEventHandlers;
  onProcessed?: (result: WebhookResult, payload: DodoNextWebhookPayload) => void | Promise<void>;
  adapter?: DodoNextWebhookAdapter;
}

/** Build a Next.js App Router handler on top of Dodo's official adapter. */
export function createDodoNextWebhookHandler(options: DodoNextWebhookOptions) {
  const { adapter, ...coreOptions } = options;
  const createHandler: DodoNextWebhookAdapter =
    adapter ??
    ((config) =>
      Webhooks({
        ...config.eventHandlers,
        webhookKey: config.webhookKey,
        onPayload: config.onPayload,
      }));
  return createDodoNextWebhookHandlerCore({
    ...coreOptions,
    adapter: createHandler,
  });
}
