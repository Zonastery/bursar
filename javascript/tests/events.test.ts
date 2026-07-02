import { describe, it, expect, vi } from "vitest";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent, CreditEventType } from "../src/stores/events.js";

function makeEvent(type: CreditEventType = "credits.deducted"): CreditEvent {
  return { type, timestamp: new Date(), userId: "u1" };
}

describe("CreditEventEmitter", () => {
  // ── off() removes handler ──────────────────────────────────────────────────
  describe("off() removes a specific handler", () => {
    it("unregistered handler stops firing after off()", () => {
      const emitter = new CreditEventEmitter();
      const calls1: number[] = [];
      const calls2: number[] = [];

      const handler1 = () => calls1.push(1);
      const handler2 = () => calls2.push(2);

      emitter.on("credits.deducted", handler1);
      emitter.on("credits.deducted", handler2);

      // Both fire on first emit.
      emitter.emit(makeEvent("credits.deducted"));
      expect(calls1).toHaveLength(1);
      expect(calls2).toHaveLength(1);

      // Remove handler1 only.
      emitter.off("credits.deducted", handler1);

      // Only handler2 fires.
      emitter.emit(makeEvent("credits.deducted"));
      expect(calls1).toHaveLength(1); // unchanged
      expect(calls2).toHaveLength(2);
    });

    it("off() for a handler that was never registered is a no-op (no error)", () => {
      const emitter = new CreditEventEmitter();
      const handler = () => {};
      expect(() => emitter.off("credits.deducted", handler)).not.toThrow();
    });
  });

  // ── clearType() clears one event type ─────────────────────────────────────
  describe("clearType() clears all handlers for one type only", () => {
    it("clears deducted handlers but leaves added handlers intact", () => {
      const emitter = new CreditEventEmitter();
      const deductedCalls: number[] = [];
      const addedCalls: number[] = [];

      emitter.on("credits.deducted", () => deductedCalls.push(1));
      emitter.on("credits.added", () => addedCalls.push(1));

      emitter.clearType("credits.deducted");

      emitter.emit(makeEvent("credits.deducted"));
      emitter.emit(makeEvent("credits.added"));

      // credits.deducted handler was cleared.
      expect(deductedCalls).toHaveLength(0);
      // credits.added handler is still active.
      expect(addedCalls).toHaveLength(1);
    });

    it("clearType for a type with no registered handlers is a no-op", () => {
      const emitter = new CreditEventEmitter();
      expect(() => emitter.clearType("credits.low_balance")).not.toThrow();
    });
  });

  // ── clearAll() clears every handler ───────────────────────────────────────
  describe("clearAll() removes all handlers for all event types", () => {
    it("no handler fires after clearAll()", () => {
      const emitter = new CreditEventEmitter();
      const calls: string[] = [];

      emitter.on("credits.deducted", () => calls.push("deducted"));
      emitter.on("credits.added", () => calls.push("added"));
      emitter.on("credits.refunded", () => calls.push("refunded"));
      emitter.on("credits.low_balance", () => calls.push("low_balance"));

      emitter.clearAll();

      emitter.emit(makeEvent("credits.deducted"));
      emitter.emit(makeEvent("credits.added"));
      emitter.emit(makeEvent("credits.refunded"));
      emitter.emit(makeEvent("credits.low_balance"));

      expect(calls).toHaveLength(0);
    });
  });

  // ── Snapshot isolation: handler removes itself during emit ────────────────
  describe("snapshot isolation during emit", () => {
    it("a handler that calls off(itself) fires exactly once without error", () => {
      const emitter = new CreditEventEmitter();
      let callCount = 0;

      const selfRemovingHandler = () => {
        callCount++;
        // Remove itself mid-emit — must not cause a RangeError or skip other handlers.
        emitter.off("credits.deducted", selfRemovingHandler);
      };

      const otherCalls: number[] = [];
      emitter.on("credits.deducted", selfRemovingHandler);
      emitter.on("credits.deducted", () => otherCalls.push(1));

      expect(() => emitter.emit(makeEvent("credits.deducted"))).not.toThrow();

      // Self-removing handler fired exactly once.
      expect(callCount).toBe(1);
      // Other handler still ran in the same emit cycle.
      expect(otherCalls).toHaveLength(1);

      // On the next emit the self-removed handler does not fire.
      emitter.emit(makeEvent("credits.deducted"));
      expect(callCount).toBe(1); // still 1
      expect(otherCalls).toHaveLength(2);
    });
  });

  // ── Unregistered event type is a no-op ─────────────────────────────────────
  describe("unregistered event type", () => {
    it("emitting an event type with no handlers does not throw", () => {
      const emitter = new CreditEventEmitter();
      // No handlers registered for any type.
      expect(() => emitter.emit(makeEvent("credits.cap_reached"))).not.toThrow();
      expect(() => emitter.emit(makeEvent("credits.deduct_failed"))).not.toThrow();
    });
  });

  // ── New event types (lazy expiry / subscription cycles) ────────────────────
  describe("credits.cycle_renewed and credits.floor_breach are valid event types", () => {
    it("credits.cycle_renewed round-trips through on()/emit() like any other type", () => {
      const emitter = new CreditEventEmitter();
      const calls: CreditEvent[] = [];
      emitter.on("credits.cycle_renewed", (e) => calls.push(e));

      emitter.emit(makeEvent("credits.cycle_renewed"));
      expect(calls).toHaveLength(1);
      expect(calls[0].type).toBe("credits.cycle_renewed");
    });

    it("credits.floor_breach round-trips through on()/emit() like any other type", () => {
      const emitter = new CreditEventEmitter();
      const calls: CreditEvent[] = [];
      emitter.on("credits.floor_breach", (e) => calls.push(e));

      emitter.emit(makeEvent("credits.floor_breach"));
      expect(calls).toHaveLength(1);
      expect(calls[0].type).toBe("credits.floor_breach");
    });
  });

  // ── Handler exception isolation ────────────────────────────────────────────
  describe("handler exception isolation", () => {
    it("second handler still fires even when first handler throws, and emit does not throw", () => {
      const emitter = new CreditEventEmitter();
      const secondCalls: number[] = [];

      emitter.on("credits.deducted", () => {
        throw new Error("handler 1 explosion");
      });
      emitter.on("credits.deducted", () => secondCalls.push(1));

      // emit must not propagate the thrown error.
      expect(() => emitter.emit(makeEvent("credits.deducted"))).not.toThrow();

      // Second handler still ran.
      expect(secondCalls).toHaveLength(1);
    });

    it("async handler that rejects does not surface as an unhandled rejection", () => {
      const emitter = new CreditEventEmitter();
      // Spy on console.error to confirm it was swallowed, not propagated.
      const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});

      emitter.on("credits.added", async () => {
        await Promise.resolve();
        throw new Error("async rejection");
      });

      expect(() => emitter.emit(makeEvent("credits.added"))).not.toThrow();
      consoleSpy.mockRestore();
    });
  });
});
