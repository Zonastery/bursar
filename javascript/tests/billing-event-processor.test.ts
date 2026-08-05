import { describe, expect, it, vi } from "vitest";
import type { BillingStore } from "../src/billing/billing-store.js";
import {
  boundedDiagnosticMessage,
  optionalBoundedDiagnosticMessage,
} from "../src/shared/diagnostics.js";
import { BillingEventProcessor } from "../src/billing/event-processor.js";
import { BillingEventRepository } from "../src/billing/postgres/repositories/event.js";
import type { BillingEvent } from "../src/billing/types/index.js";
import { BillingEventType } from "../src/billing/types/index.js";

const CLAIM_TOKEN = "00000000-0000-0000-0000-000000000003";
const BILLING_EVENT_ID = "00000000-0000-0000-0000-000000000004";

function claimedStore() {
  return {
    claimBillingEvent: vi.fn().mockResolvedValue({
      status: "claimed",
      claimToken: CLAIM_TOKEN,
      billingEventId: BILLING_EVENT_ID,
    }),
    completeBillingEvent: vi.fn().mockResolvedValue(true),
    failBillingEvent: vi.fn().mockResolvedValue(true),
    upsertBillingCustomer: vi.fn().mockResolvedValue(undefined),
  };
}

function event(eventId: string, eventType: BillingEventType): BillingEvent {
  return {
    provider: "stripe",
    eventId,
    eventType,
    occurredAt: "2026-08-05T00:00:00Z",
  };
}

describe("BillingEventProcessor lifecycle acknowledgements", () => {
  it("reports and requeues a rejected completion", async () => {
    const store = claimedStore();
    store.completeBillingEvent.mockResolvedValue(false);
    const processor = new BillingEventProcessor(store as unknown as BillingStore);

    const result = await processor.ingestBillingEvent(
      event("evt_completion_rejected", BillingEventType.INVOICE_UPCOMING),
    );

    expect(result).toEqual({ handled: false, error: "billing_event_completion_rejected" });
    expect(store.failBillingEvent).toHaveBeenCalledWith(
      "stripe",
      "evt_completion_rejected",
      CLAIM_TOKEN,
      "billing_event_completion_rejected",
    );
  });

  it("fails an unhandled event instead of completing it", async () => {
    const store = claimedStore();
    const processor = new BillingEventProcessor(store as unknown as BillingStore);

    const result = await processor.ingestBillingEvent({
      ...event("evt_unhandled", BillingEventType.INVOICE_UPCOMING),
      eventType: "provider.unknown" as BillingEventType,
    });

    expect(result).toEqual({ handled: false, error: "unhandled_event_type" });
    expect(store.completeBillingEvent).not.toHaveBeenCalled();
    expect(store.failBillingEvent).toHaveBeenCalledWith(
      "stripe",
      "evt_unhandled",
      CLAIM_TOKEN,
      "unhandled_event_type",
    );
  });

  it.each([
    ["   ", "Error"],
    [`  ${"x".repeat(9_000)}  `, "x".repeat(8_192)],
  ])("normalizes processing error %j before persistence", async (rawMessage, expected) => {
    const store = claimedStore();
    store.upsertBillingCustomer.mockRejectedValue(new Error(rawMessage));
    const processor = new BillingEventProcessor(store as unknown as BillingStore);

    const result = await processor.ingestBillingEvent({
      ...event("evt_failure_message", BillingEventType.CUSTOMER_CREATED),
      userId: "00000000-0000-0000-0000-000000000001",
      customer: { providerCustomerId: "cus_failure" },
    });

    expect(result).toEqual({ handled: false, error: expected });
    expect(store.failBillingEvent).toHaveBeenCalledWith(
      "stripe",
      "evt_failure_message",
      CLAIM_TOKEN,
      expected,
    );
  });
});

describe("billing diagnostic and repository boundaries", () => {
  it("preserves absent diagnostics and removes NUL characters", () => {
    expect(optionalBoundedDiagnosticMessage(null)).toBeNull();
    expect(boundedDiagnosticMessage("  failed\0message  ")).toBe("failed\uFFFDmessage");
  });

  it("returns lifecycle RPC acknowledgements", async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce([{ completed: true }])
      .mockResolvedValueOnce([{ failed: false }]);
    const repository = new BillingEventRepository(query);

    await expect(repository.complete("stripe", "evt_repository", CLAIM_TOKEN)).resolves.toBe(true);
    await expect(repository.fail("stripe", "evt_repository", CLAIM_TOKEN, "failed")).resolves.toBe(
      false,
    );
  });
});
