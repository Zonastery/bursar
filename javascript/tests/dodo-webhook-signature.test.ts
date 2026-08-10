import { NotFoundError } from "dodopayments";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { BillingEventSink } from "../src/billing/contracts.js";
import type { DodoClient } from "../src/providers/dodo/client-contract.js";
import { DodoProvider } from "../src/providers/dodo/provider.js";
import type { WebhookRequest } from "../src/providers/types.js";
import { DODO_ISO_DATE, dodoEventId } from "./helpers/dodo-fixtures.js";

const ingestBillingEvent = vi
  .fn<BillingEventSink["ingestBillingEvent"]>()
  .mockResolvedValue({ handled: true });
const mockSink: BillingEventSink = { ingestBillingEvent };

const mockLogger = {
  debug: vi.fn(),
  info: vi.fn(),
  warn: vi.fn(),
  error: vi.fn(),
};

const WEBHOOK_KEY = "test_wh_key_12345";
const ACCOUNT_ID = "team-account-1";

function webhookProvider(unwrap: DodoClient["webhooks"]["unwrap"]): DodoProvider {
  const client = { webhooks: { unwrap } } as unknown as DodoClient;
  return new DodoProvider({
    getClient: () => client,
    webhookKey: WEBHOOK_KEY,
    eventSink: mockSink,
    logger: mockLogger,
  });
}

describe("DodoProvider webhook signature verification", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns received:true when the SDK unwrap succeeds", async () => {
    const unwrap = vi.fn().mockReturnValue({
      type: "subscription.active",
      timestamp: DODO_ISO_DATE,
      data: {
        id: "evt_test_valid",
        subscription_id: "sub_test_valid",
        metadata: { bursar_account_id: ACCOUNT_ID, plan_slug: "monk" },
      },
    });
    const provider = webhookProvider(unwrap);

    const req: WebhookRequest = {
      rawBody: JSON.stringify({
        type: "subscription.active",
        data: { metadata: { bursar_account_id: ACCOUNT_ID } },
      }),
      headers: {
        "content-type": "application/json",
        "x-webhook-signature": "valid_signature_here",
      },
    };

    const result = await provider.handleWebhook(req);
    expect(result).toEqual({
      received: true,
      retryable: false,
      provider: "dodo",
      eventId: dodoEventId("subscription.active", "sub_test_valid"),
      eventType: "subscription.active",
    });
    expect(unwrap).toHaveBeenCalledWith(req.rawBody, {
      headers: req.headers,
      key: WEBHOOK_KEY,
    });
    expect(ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ accountId: ACCOUNT_ID }),
    );
  });

  it("treats the SDK-verified payload as authoritative", async () => {
    const unwrap = vi.fn().mockReturnValue({
      type: "subscription.active",
      timestamp: DODO_ISO_DATE,
      data: {
        id: "evt_signed_extensions",
        subscription_id: "sub_signed_extensions",
        metadata: { userId: ACCOUNT_ID },
      },
    });
    const provider = webhookProvider(unwrap);

    await provider.handleWebhook({
      rawBody: JSON.stringify({
        type: "subscription.active",
        data: {
          metadata: { bursar_account_id: ACCOUNT_ID },
          product_cart: [{ product_id: "pdt_signed_extensions" }],
        },
      }),
      headers: { "webhook-signature": "verified-by-unwrap" },
    });

    const event = ingestBillingEvent.mock.calls[0]?.[0];
    expect(event?.accountId).toBeUndefined();
    expect(event?.subscription?.refs).toBeUndefined();
  });

  it("processes an already verified payload without invoking SDK verification", async () => {
    const unwrap = vi.fn(() => {
      throw new Error("verified payloads must not be unwrapped again");
    });
    const provider = webhookProvider(unwrap);

    const result = await provider.handleVerifiedWebhook({
      type: "subscription.active",
      timestamp: DODO_ISO_DATE,
      data: {
        id: "evt_adapter_verified",
        subscription_id: "sub_adapter_verified",
        metadata: { bursar_account_id: ACCOUNT_ID, plan_slug: "monk" },
      },
    });

    expect(result).toEqual({
      received: true,
      retryable: false,
      provider: "dodo",
      eventId: dodoEventId("subscription.active", "sub_adapter_verified"),
      eventType: "subscription.active",
    });
    expect(unwrap).not.toHaveBeenCalled();
    expect(ingestBillingEvent).toHaveBeenCalled();
  });

  it("returns received:false retryable:false on signature verification failure", async () => {
    const unwrap = vi.fn(() => {
      throw new Error("Invalid signature");
    });
    const provider = webhookProvider(unwrap);

    const req: WebhookRequest = {
      rawBody: JSON.stringify({ type: "subscription.active", data: {} }),
      headers: {
        "content-type": "application/json",
        "x-webhook-signature": "tampered_signature",
      },
    };

    const result = await provider.handleWebhook(req);
    expect(result).toEqual({
      received: false,
      retryable: false,
      provider: "dodo",
      eventId: null,
      eventType: null,
    });
  });

  it("returns non-retryable when the signed payload is malformed", async () => {
    const unwrap = vi.fn(() => {
      throw new SyntaxError("Malformed payload");
    });
    const provider = webhookProvider(unwrap);

    const req: WebhookRequest = {
      rawBody: JSON.stringify({ type: "subscription.active", data: {} }),
      headers: { "content-type": "application/json" },
    };

    const result = await provider.handleWebhook(req);
    expect(result).toEqual({
      received: false,
      retryable: false,
      provider: "dodo",
      eventId: null,
      eventType: null,
    });
  });

  it("leaves metadata-free payment failures for persisted-reference resolution", async () => {
    const unwrap = vi.fn().mockReturnValue({
      type: "payment.failed",
      timestamp: DODO_ISO_DATE,
      data: {
        id: "evt_payment_failed",
        payment_id: "pay_failed",
        total_amount: 500,
        currency: "USD",
        customer: { customer_id: "cus_failed", email: "guest@example.com" },
      },
    });
    const provider = webhookProvider(unwrap);

    const result = await provider.handleWebhook({
      rawBody: JSON.stringify({
        type: "payment.failed",
        data: { payment_id: "pay_failed" },
      }),
      headers: {},
    });

    expect(result).toEqual({
      received: true,
      retryable: false,
      provider: "dodo",
      eventId: dodoEventId("payment.failed", "pay_failed"),
      eventType: "payment.failed",
    });
    const event = ingestBillingEvent.mock.calls[0]?.[0];
    expect(event).toMatchObject({ eventType: "payment.failed" });
    expect(event).not.toHaveProperty("accountId");
  });

  it("retrieves checkout payment status and treats a missing session as expired", async () => {
    const retrieve = vi
      .fn()
      .mockResolvedValueOnce({ payment_status: "requires_customer_action" })
      .mockRejectedValueOnce(new NotFoundError(404, {}, "not found", new Headers()));
    const client = { checkoutSessions: { retrieve } } as unknown as DodoClient;
    const provider = new DodoProvider({
      getClient: () => client,
      webhookKey: WEBHOOK_KEY,
      eventSink: mockSink,
    });

    await expect(provider.getCheckoutSessionStatus("cks_1")).resolves.toEqual({
      paymentStatus: "requires_customer_action",
    });
    await expect(provider.getCheckoutSessionStatus("cks_missing")).resolves.toBeNull();
  });
});
