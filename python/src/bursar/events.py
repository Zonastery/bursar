"""Typed event emitter for credit lifecycle events.

Events are emitted by ``CreditManager`` after each store operation.
The emitter is optional — inject into ``CreditManager`` constructor,
no-op if omitted.

Usage::

    from bursar.events import CreditEventEmitter

    emitter = CreditEventEmitter()
    emitter.on("credits.deducted", lambda event: print(event))
    manager = CreditManager(store=store, emitter=emitter)
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# All credit lifecycle event types.
#
# Success events fire only after the underlying store operation committed; the
# failure events (``credits.deduct_failed``/``credits.refund_failed``) fire on a
# business error so a billing system can observe denials/over-refunds (contract
# §6, H3). Event payload money is ``Decimal``.
CREDIT_EVENT_TYPES = frozenset(
    {
        "credits.deducted",
        "credits.deduct_failed",
        "credits.added",
        "credits.refunded",
        "credits.refund_failed",
        "credits.expired",
        "credits.cap_reached",
        "credits.cap_warning",
        "credits.feature_limit_reached",
        "credits.feature_limit_warning",
        "credits.low_balance",
        "credits.plan_changed",
        # Lease lifecycle (interface plan §3/M4): make admission, release, expiry
        # and overdraft observable end-to-end.
        "credits.reserved",
        "credits.reservation_released",
        "credits.lease_expired",
        "credits.overdraft",
        # Non-blocking signal emitted by _post_charge_signals when a settle/deduct
        # leaves the balance below min_balance without going negative (manager.py).
        # Pre-existing gap: this was emitted but missing from this frozenset.
        "credits.floor_breach",
        # Emitted by CreditManager.grant_subscription_cycle (webhook renewal/signup
        # grant helper).
        "credits.cycle_renewed",
    }
)

CreditEventType = str


class CreditEvent(BaseModel):
    """A typed credit lifecycle event."""

    type: CreditEventType
    timestamp: datetime
    user_id: str
    data: dict[str, Any] | None = None


EventHandler = Callable[[CreditEvent], None]


class CreditEventEmitter:
    """Typed pub/sub event emitter for credit events.

    Handlers are registered by event type string and called synchronously
    when the event is emitted. No-op when no handlers are registered.

    Thread-safety: ``_lock`` (a ``threading.RLock``) guards all mutations to
    ``_listeners``.  The emit loop copies the handler list under the lock then
    calls each handler *outside* the lock so a handler that calls ``on()`` or
    ``off()`` during dispatch cannot deadlock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: dict[CreditEventType, list[EventHandler]] = {}

    def on(self, event_type: CreditEventType, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        with self._lock:
            if event_type not in self._listeners:
                self._listeners[event_type] = []
            self._listeners[event_type].append(handler)

    def off(self, event_type: CreditEventType, handler: EventHandler) -> None:
        """Remove a previously registered handler."""
        with self._lock:
            handlers = self._listeners.get(event_type)
            if handlers:
                with suppress(ValueError):
                    handlers.remove(handler)

    def emit(self, event: CreditEvent) -> None:
        """Emit an event to all registered handlers.

        The handler list is snapshot-copied under the lock so that handlers
        can safely call ``on()``/``off()`` without risk of deadlock or
        iterator corruption.
        """
        with self._lock:
            handlers = list(self._listeners.get(event.type) or [])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("Credit event handler failed for event %s", event.type)

    def clear_type(self, event_type: CreditEventType) -> None:
        """Remove all handlers for a specific type."""
        with self._lock:
            self._listeners.pop(event_type, None)

    def clear_all(self) -> None:
        """Remove all handlers for all types."""
        with self._lock:
            self._listeners.clear()
