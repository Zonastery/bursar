"""Contract tests for the thread-safe credit event emitter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from bursar.events import CreditEvent, CreditEventEmitter


def _event(event_type: str = "credits.deducted") -> CreditEvent:
    return CreditEvent(type=event_type, timestamp=datetime.now(UTC), user_id="u1", data={"amount": "1"})


def test_handlers_are_snapshot_copied_and_failures_are_isolated() -> None:
    emitter = CreditEventEmitter()
    seen: list[str] = []

    def late(event: CreditEvent) -> None:
        seen.append(f"late:{event.user_id}")

    def first(event: CreditEvent) -> None:
        seen.append("first")
        emitter.off("credits.deducted", first)
        emitter.on("credits.deducted", late)

    def broken(_event: CreditEvent) -> None:
        raise RuntimeError("listener failure")

    emitter.on("credits.deducted", first)
    emitter.on("credits.deducted", broken)

    emitter.emit(_event())
    assert seen == ["first"]
    emitter.emit(_event())
    assert seen == ["first", "late:u1"]


def test_unknown_event_type_is_rejected() -> None:
    emitter = CreditEventEmitter()
    with pytest.raises(ValueError):
        emitter.emit(_event("not-a-credit-event"))  # type: ignore[arg-type]


def test_concurrent_registration_and_emission_is_safe() -> None:
    emitter = CreditEventEmitter()
    seen: list[int] = []

    def listener(_event: CreditEvent) -> None:
        seen.append(1)

    def register_and_emit(_: int) -> None:
        emitter.on("credits.deducted", listener)
        emitter.emit(_event())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register_and_emit, range(32)))

    assert len(seen) >= 32
    emitter.clear_all()
    before = len(seen)
    emitter.emit(_event())
    assert len(seen) == before
