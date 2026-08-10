import { beforeEach, describe, expect, it, vi } from "vitest";

const adapterMocks = vi.hoisted(() => ({
  nextWebhooks: vi.fn(),
  betterAuthWebhooks: vi.fn(),
}));

vi.mock("@dodopayments/nextjs", () => ({
  Webhooks: adapterMocks.nextWebhooks,
}));

vi.mock("@dodopayments/better-auth", () => ({
  webhooks: adapterMocks.betterAuthWebhooks,
}));

import { createDodoNextWebhookHandler } from "../src/providers/dodo/nextjs.js";
import { dodoBetterAuthWebhooks } from "../src/providers/dodo/better-auth.js";
import type { BillingEventSink } from "../src/billing/contracts.js";
import { DODO_ISO_DATE, dodoEventId } from "./helpers/dodo-fixtures.js";

const ACCOUNT_ID = "team-account-1";

function verifiedPayload() {
  return {
    type: "subscription.active",
    timestamp: DODO_ISO_DATE,
    data: {
      id: "evt_framework_adapter",
      subscription_id: "sub_framework_adapter",
      metadata: { bursar_account_id: ACCOUNT_ID, plan_slug: "monk" },
    },
  };
}

describe("official Dodo framework integrations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("builds the official Next.js handler and ingests its verified payload", async () => {
    const officialHandler = vi.fn();
    adapterMocks.nextWebhooks.mockReturnValue(officialHandler);
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
    });

    expect(handler).toBe(officialHandler);
    const config = adapterMocks.nextWebhooks.mock.calls[0]?.[0] as {
      webhookKey: string;
      onPayload(payload: ReturnType<typeof verifiedPayload>): Promise<void>;
      onSubscriptionActive: typeof onSubscriptionActive;
    };
    expect(config.webhookKey).toBe("whsec_next");
    expect(config.onSubscriptionActive).toBe(onSubscriptionActive);

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
    const officialPlugin = vi.fn();
    adapterMocks.betterAuthWebhooks.mockReturnValue(officialPlugin);
    const ingestBillingEvent = vi
      .fn<BillingEventSink["ingestBillingEvent"]>()
      .mockResolvedValue({ handled: true });

    const plugin = dodoBetterAuthWebhooks({
      webhookKey: "whsec_better_auth",
      getEventSink: () => ({ ingestBillingEvent }),
    });

    expect(plugin).toBe(officialPlugin);
    const config = adapterMocks.betterAuthWebhooks.mock.calls[0]?.[0] as {
      webhookKey: string;
      onPayload(payload: ReturnType<typeof verifiedPayload>): Promise<void>;
    };
    expect(config.webhookKey).toBe("whsec_better_auth");

    await config.onPayload(verifiedPayload());

    expect(ingestBillingEvent).toHaveBeenCalled();
  });
});
