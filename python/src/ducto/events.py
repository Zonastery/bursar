"""Typed event emitter for credit lifecycle events.

Events are emitted by ``CreditManager`` after each store operation.
The emitter is optional — inject into ``CreditManager`` constructor,
no-op if omitted.

Usage::

    from ducto.events import CreditEventEmitter

    emitter = CreditEventEmitter()
    emitter.on("credits.deducted", lambda event: print(event))
    manager = CreditManager(store=store, emitter=emitter)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# All credit lifecycle event types
CREDIT_EVENT_TYPES = frozenset(
    {
        "credits.deducted",
        "credits.added",
        "credits.refunded",
        "credits.expired",
        "credits.cap_reached",
        "credits.cap_warning",
        "credits.low_balance",
        "credits.plan_changed",
    }
)

CreditEventType = str


@dataclass
class CreditEvent:
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
    """

    def __init__(self) -> None:
        self._listeners: dict[CreditEventType, list[EventHandler]] = {}

    def on(self, event_type: CreditEventType, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(handler)

    def off(self, event_type: CreditEventType, handler: EventHandler) -> None:
        """Remove a previously registered handler."""
        handlers = self._listeners.get(event_type)
        if handlers:
            with suppress(ValueError):
                handlers.remove(handler)

    def emit(self, event: CreditEvent) -> None:
        """Emit an event to all registered handlers."""
        handlers = self._listeners.get(event.type)
        if handlers:
            for handler in handlers:
                try:
                    handler(event)
                except Exception:
                    logger.exception("Credit event handler failed for event %s", event.type)

    def clear_type(self, event_type: CreditEventType) -> None:
        """Remove all handlers for a specific type."""
        self._listeners.pop(event_type, None)

    def clear_all(self) -> None:
        """Remove all handlers for all types."""
        self._listeners.clear()
