import { beforeEach, describe, expect, it, vi } from "vitest";

const sdkMocks = vi.hoisted(() => ({
  start: vi.fn(),
  getAbandonedSession: vi.fn(),
  clearAbandonedSession: vi.fn(),
  handleOpenURL: vi.fn(),
}));

vi.mock("@dodopayments/react-native-checkout", () => ({
  DodoCheckout: sdkMocks,
}));

import {
  createDodoReactNativeCheckout,
  type DodoReactNativeCheckoutStore,
  type DodoReactNativePendingCheckout,
} from "../src/providers/dodo/react-native.js";

function memoryStore() {
  let pending: DodoReactNativePendingCheckout | null = null;
  const store: DodoReactNativeCheckoutStore = {
    getPendingCheckout: vi.fn(async () => pending),
    setPendingCheckout: vi.fn(async (value) => {
      pending = value;
    }),
    clearPendingCheckout: vi.fn(async () => {
      pending = null;
    }),
  };
  return { store, current: () => pending };
}

describe("Dodo React Native integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sdkMocks.start.mockResolvedValue({ status: "cancelled", raw: {} });
    sdkMocks.getAbandonedSession.mockResolvedValue(null);
    sdkMocks.clearAbandonedSession.mockResolvedValue(undefined);
    sdkMocks.handleOpenURL.mockResolvedValue(true);
  });

  it("persists the Bursar intent and delegates presentation to the official SDK", async () => {
    const { store, current } = memoryStore();
    const onEvent = vi.fn();
    const getCheckoutStatus = vi.fn().mockResolvedValue("pending");
    const checkout = createDodoReactNativeCheckout({
      returnUrl: "zonastery-dev-checkout://return",
      store,
      getCheckoutStatus,
      onEvent,
    });

    await expect(
      checkout.start({
        intentId: "intent-1",
        url: "https://checkout.dodopayments.com/session/cks_1",
      }),
    ).resolves.toEqual({
      intentId: "intent-1",
      result: { status: "cancelled", raw: {} },
    });

    expect(current()).toEqual({
      intentId: "intent-1",
      startedAt: expect.any(String),
    });
    expect(sdkMocks.start).toHaveBeenCalledWith({
      checkoutUrl: "https://checkout.dodopayments.com/session/cks_1",
      returnUrl: "zonastery-dev-checkout://return",
      onEvent,
    });
    await expect(checkout.reconcile()).resolves.toMatchObject({
      confirmationStatus: "pending",
    });
    expect(current()).not.toBeNull();
    expect(sdkMocks.clearAbandonedSession).not.toHaveBeenCalled();
  });

  it("clears both recovery records only after Bursar reports a terminal status", async () => {
    const { store, current } = memoryStore();
    await store.setPendingCheckout({
      intentId: "intent-2",
      startedAt: "2026-08-07T00:00:00.000Z",
    });
    sdkMocks.getAbandonedSession.mockResolvedValue({
      sessionId: "cks_2",
      createdAt: new Date("2026-08-07T00:00:00.000Z"),
    });
    const getCheckoutStatus = vi.fn().mockResolvedValue("succeeded");
    const checkout = createDodoReactNativeCheckout({
      returnUrl: "zonastery-dev-checkout://return",
      store,
      getCheckoutStatus,
    });

    await expect(checkout.reconcile()).resolves.toMatchObject({
      pending: { intentId: "intent-2" },
      abandoned: { sessionId: "cks_2" },
      confirmationStatus: "succeeded",
    });
    expect(getCheckoutStatus).toHaveBeenCalledWith("intent-2");
    expect(current()).toBeNull();
    expect(sdkMocks.clearAbandonedSession).toHaveBeenCalledOnce();
    await expect(checkout.handleOpenURL("zonastery-dev-checkout://return")).resolves.toBe(true);
  });
});
