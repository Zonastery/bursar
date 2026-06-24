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

/** All credit lifecycle event types. */
export type CreditEventType =
  | "credits.deducted"
  | "credits.added"
  | "credits.refunded"
  | "credits.expired"
  | "credits.cap_reached"
  | "credits.cap_warning"
  | "credits.low_balance"
  | "credits.plan_changed";

/** A typed credit lifecycle event. */
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

  /** Emit an event to all registered handlers. No-op if no handlers exist. */
  emit(event: CreditEvent): void {
    const handlers = this.listeners.get(event.type);
    if (handlers) {
      for (const handler of handlers) {
        try {
          handler(event);
        } catch (err) {
          console.error(`[CreditEventEmitter] handler error for event ${event.type}:`, err);
        }
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
