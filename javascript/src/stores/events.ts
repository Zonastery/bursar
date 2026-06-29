/**
 * Typed event emitter for credit lifecycle events.
 *
 * Events are emitted by ``CreditManager`` after each store operation.
 * The emitter is optional — inject into ``CreditManager`` constructor,
 * no-op if omitted.
 *
 * @example
 * ```ts
 * const emitter = new CreditEventEmitter();
 * emitter.on("credits.deducted", (event) => {
 *   console.log(`Deducted ${event.data?.amount} from ${event.userId}`);
 * });
 * const manager = new CreditManager(store, engine, emitter);
 * ```
 */

/**
 * All credit lifecycle event types.
 *
 * Success events (``credits.deducted``/``credits.refunded``/…) are emitted by
 * ``CreditManager`` only after the underlying store operation committed without
 * an ``error`` (contract §6). Failure events (``credits.deduct_failed`` /
 * ``credits.refund_failed``) carry the store's business-error code in
 * ``data.error`` for observability/fraud monitoring.
 */
export type CreditEventType =
  | "credits.deducted"
  | "credits.deduct_failed"
  | "credits.added"
  | "credits.refunded"
  | "credits.refund_failed"
  | "credits.expired"
  | "credits.cap_reached"
  | "credits.cap_warning"
  | "credits.low_balance"
  | "credits.plan_changed";

/**
 * A typed credit lifecycle event.
 *
 * Money values inside ``data`` (``amount``, ``balanceAfter``,
 * ``allowanceConsumed``, ``threshold``, …) are exact `Decimal` instances
 * (contract §1/§6), never binary `number`.
 */
export interface CreditEvent {
  type: CreditEventType;
  timestamp: Date;
  userId: string;
  data?: Record<string, unknown>;
}

type EventHandler = (event: CreditEvent) => void;

/** Typed pub/sub event emitter for credit events. */
export class CreditEventEmitter {
  private listeners = new Map<CreditEventType, Set<EventHandler>>();

  /** Register a handler for a specific event type. */
  on(type: CreditEventType, handler: EventHandler): void {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set());
    }
    this.listeners.get(type)!.add(handler);
  }

  /** Remove a previously registered handler. */
  off(type: CreditEventType, handler: EventHandler): void {
    this.listeners.get(type)?.delete(handler);
  }

  /**
   * Emit an event to all registered handlers. No-op if no handlers exist.
   *
   * Each handler is isolated: a synchronous throw (or a rejected promise from an
   * async handler) is caught and logged, never propagated. This guarantees a
   * misbehaving listener can never break the manager's main flow and never
   * produces an unhandled promise rejection (contract §6).
   */
  emit(event: CreditEvent): void {
    const handlers = this.listeners.get(event.type);
    if (!handlers) return;
    // Iterate a snapshot so a handler that mutates the set during emit is safe.
    for (const handler of [...handlers]) {
      try {
        const out = handler(event) as unknown;
        // Swallow rejections from async handlers so they never become unhandled.
        if (out instanceof Promise) {
          out.catch((err: unknown) => {
            console.error(
              `[CreditEventEmitter] async handler error for event ${event.type}:`,
              err,
            );
          });
        }
      } catch (err) {
        console.error(`[CreditEventEmitter] handler error for event ${event.type}:`, err);
      }
    }
  }

  /** Remove all handlers for a specific type. */
  clearType(type: CreditEventType): void {
    this.listeners.delete(type);
  }

  /** Remove all handlers for all types. */
  clearAll(): void {
    this.listeners.clear();
  }
}
