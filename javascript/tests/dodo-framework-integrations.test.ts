import { describe, expect, it, vi } from "vitest";

import {
  createDodoNextWebhookHandlerCore as createDodoNextWebhookHandler,
  type DodoNextWebhookAdapter,
  type DodoNextWebhookAdapterConfig,
  type DodoNextWebhookHandler,
} from "../src/providers/dodo/nextjs-core.js";
import {
  dodoBetterAuthWebhooksCore as dodoBetterAuthWebhooks,
  type DodoBetterAuthWebhookAdapter,
  type DodoBetterAuthWebhookAdapterConfig,
  type DodoBetterAuthWebhookPlugin,
} from "../src/providers/dodo/better-auth-core.js";
import type { BillingEventSink } from "../src/billing/contracts.js";
import { DODO_ISO_DATE, dodoEventId } from "./helpers/dodo-fixtures.js";

const ACCOUNT_ID = "team-account-1";

function testNextHandler<THandler>(handler: THandler): DodoNextWebhookHandler {
  // SAFETY: The framework adapter test never invokes the returned route handler.
  return handler as DodoNextWebhookHandler;
}

function testBetterAuthPlugin<TPlugin>(plugin: TPlugin): DodoBetterAuthWebhookPlugin {
  // SAFETY: The framework adapter test only verifies the plugin configuration bridge.
  return plugin as DodoBetterAuthWebhookPlugin;
}

function verifiedPayload() {
  return {
    type: "subscription.active",
    business_id: "business_framework_adapter",
    timestamp: new Date(DODO_ISO_DATE),
    data: {
      id: "evt_framework_adapter",
      subscription_id: "sub_framework_adapter",
      metadata: { bursar_account_id: ACCOUNT_ID, plan_slug: "monk" },
    },
  };
}

describe("official Dodo framework integrations", () => {
  it("builds the official Next.js handler and ingests its verified payload", async () => {
    const captured: DodoNextWebhookAdapterConfig[] = [];
    const adapter: DodoNextWebhookAdapter = (config) => {
      captured.push(config);
      return testNextHandler(async () => new Response());
    };
    const ingestBillingEvent = vi
      .fn<BillingEventSink["ingestBillingEvent"]>()
      .mockResolvedValue({ handled: true });
    const onProcessed = vi.fn();
    const onSubscriptionActive = vi.fn();

    const handler = createDodoNextWebhookHandler({
      webhookKey: "whsec_next",
      getEventSink: async () => ({ ingestBillingEvent }),
      eventHandlers: { onSubscriptionActive },
      onProcessed,
      adapter,
    });
    expect(handler).toBeDefined();

    const config = captured[0];
    expect(config).toBeDefined();
    if (!config) throw new Error("Next adapter did not receive its configuration");
    expect(config.webhookKey).toBe("whsec_next");
    expect(config.eventHandlers?.onSubscriptionActive).toBe(onSubscriptionActive);

    const payload = verifiedPayload();
    await config.onPayload(payload);

    expect(ingestBillingEvent).toHaveBeenCalled();
    expect(onProcessed).toHaveBeenCalledWith(
      expect.objectContaining({
        received: true,
        provider: "dodo",
        eventId: dodoEventId("subscription.active", "sub_framework_adapter"),
      }),
      payload,
    );
  });

  it("builds the official Better Auth webhook plugin with the same Bursar bridge", async () => {
    const captured: DodoBetterAuthWebhookAdapterConfig[] = [];
    const adapter: DodoBetterAuthWebhookAdapter = (config) => {
      captured.push(config);
      return testBetterAuthPlugin(() => ({}));
    };
    const ingestBillingEvent = vi
      .fn<BillingEventSink["ingestBillingEvent"]>()
      .mockResolvedValue({ handled: true });

    const plugin = dodoBetterAuthWebhooks({
      webhookKey: "whsec_better_auth",
      getEventSink: () => ({ ingestBillingEvent }),
      adapter,
    });

    expect(plugin).toBeDefined();
    const config = captured[0];
    expect(config).toBeDefined();
    if (!config) throw new Error("Better Auth adapter did not receive its configuration");
    expect(config.webhookKey).toBe("whsec_better_auth");

    await config.onPayload(verifiedPayload());

    expect(ingestBillingEvent).toHaveBeenCalled();
  });
});
