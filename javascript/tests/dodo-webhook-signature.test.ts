import { describe, it, expect, vi, beforeAll, beforeEach, Mock } from "vitest";
import type { BillingService, BillingEventResult } from "../src/billing/index.js";
import type { WebhookRequest } from "../src/providers/types.js";

// Mock @dodopayments/core/webhook at the top level so every import picks it up
vi.mock("@dodopayments/core/webhook", () => ({
  verifyWebhookPayload: vi.fn(),
}));

const mockBm = {
  ingestBillingEvent: vi.fn<(...args: unknown[]) => BillingEventResult>().mockResolvedValue({
    handled: true,
  }),
} as unknown as BillingService;

const mockLogger = {
  debug: vi.fn(),
  info: vi.fn(),
  warn: vi.fn(),
  error: vi.fn(),
};

const WEBHOOK_KEY = "test_wh_key_12345";
const USER_ID = "00000000-0000-0000-0000-000000000001";

describe("DodoProvider webhook signature verification", () => {
  let DodoProvider: typeof import("../src/providers/dodo/provider.js").DodoProvider;

  beforeAll(async () => {
    const mod = await import("../src/providers/dodo/provider.js");
    DodoProvider = mod.DodoProvider;
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns received:true when verifyWebhookPayload succeeds", async () => {
    const { verifyWebhookPayload } = await import("@dodopayments/core/webhook");
    (verifyWebhookPayload as Mock).mockResolvedValue({
      type: "subscription.active",
      data: {
        id: "evt_test_valid",
        subscription_id: "sub_test_valid",
        metadata: { userId: USER_ID, plan_slug: "monk" },
      },
    });

    const provider = new DodoProvider(
      () => ({}) as never,
      { webhookKey: WEBHOOK_KEY },
      mockBm,
      undefined,
      mockLogger,
    );

    const req: WebhookRequest = {
      rawBody: JSON.stringify({
        type: "subscription.active",
        data: { metadata: { userId: USER_ID } },
      }),
      headers: {
        "content-type": "application/json",
        "x-webhook-signature": "valid_signature_here",
      },
    };

    const result = await provider.handleWebhook(req);
    expect(result).toEqual({ received: true });
  });

  it("returns received:false retryable:false on signature verification failure", async () => {
    const { verifyWebhookPayload } = await import("@dodopayments/core/webhook");
    (verifyWebhookPayload as Mock).mockRejectedValue(new Error("Invalid signature"));

    const provider = new DodoProvider(
      () => ({}) as never,
      { webhookKey: WEBHOOK_KEY },
      mockBm,
      undefined,
      mockLogger,
    );

    const req: WebhookRequest = {
      rawBody: JSON.stringify({ type: "subscription.active", data: {} }),
      headers: {
        "content-type": "application/json",
        "x-webhook-signature": "tampered_signature",
      },
    };

    const result = await provider.handleWebhook(req);
    expect(result).toEqual({ received: false, retryable: false });
  });

  it("returns non-retryable when verifyWebhookPayload rejects regardless of reason", async () => {
    const { verifyWebhookPayload } = await import("@dodopayments/core/webhook");
    (verifyWebhookPayload as Mock).mockRejectedValue(new Error("Network error"));

    const provider = new DodoProvider(
      () => ({}) as never,
      { webhookKey: "wrong_key" },
      mockBm,
      undefined,
      mockLogger,
    );

    const req: WebhookRequest = {
      rawBody: JSON.stringify({ type: "subscription.active", data: {} }),
      headers: { "content-type": "application/json" },
    };

    const result = await provider.handleWebhook(req);
    expect(result).toEqual({ received: false, retryable: false });
  });
});
