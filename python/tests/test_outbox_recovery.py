"""Focused parity tests for outbox correctness and recovery."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from bursar.storage import (
    OutboxDeadLetterListOptions,
    OutboxEvent,
    OutboxEventOutcome,
    OutboxRunResult,
    OutboxWorker,
    OutboxWorkerOptions,
)
from bursar.storage.postgres_repository import PostgresStorageRepository

TENANT_ID = "00000000-0000-0000-0000-000000000001"


def outbox_event(event_id: int, attempt_count: int = 1) -> OutboxEvent:
    return OutboxEvent(
        event_id=str(event_id),
        tenant_id=TENANT_ID,
        topic="usage.charge_recorded",
        aggregate_type="credit_usage_charge",
        aggregate_id=f"00000000-0000-0000-0000-{event_id:012d}",
        payload_version=1,
        payload={"secret": "must-not-reach-outcome-hooks"},
        claim_token=f"10000000-0000-0000-0000-{event_id:012d}",
        attempt_count=attempt_count,
        created_at="2026-08-10T00:00:00.000Z",
    )


class FakeStore:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events = events
        self.claim_limits: list[int] = []
        self.completed: list[OutboxEvent] = []
        self.failed: list[tuple[OutboxEvent, str, int, int]] = []
        self.renewed: list[OutboxEvent] = []
        self.complete_result = True
        self.fail_result = True
        self.renew_result = True
        self.on_renew: Callable[[], None] | None = None

    def claim(self, topics: Sequence[str], limit: int, lease_seconds: int) -> list[OutboxEvent]:
        del topics, lease_seconds
        self.claim_limits.append(limit)
        claimed = self.events[:limit]
        del self.events[:limit]
        return claimed

    def renew(self, event: OutboxEvent, lease_seconds: int) -> bool:
        del lease_seconds
        self.renewed.append(event)
        if self.on_renew is not None:
            self.on_renew()
        return self.renew_result

    def complete(self, event: OutboxEvent) -> bool:
        self.completed.append(event)
        return self.complete_result

    def fail(
        self,
        event: OutboxEvent,
        error: str,
        retry_delay_seconds: int,
        attempt_limit: int,
    ) -> bool:
        self.failed.append((event, error, retry_delay_seconds, attempt_limit))
        return self.fail_result


@dataclass(frozen=True)
class FakeHandler:
    topics: Sequence[str]
    callback: Callable[[OutboxEvent], None]

    def handle(self, event: OutboxEvent) -> None:
        self.callback(event)


def test_worker_claims_only_available_slots_within_run_budget() -> None:
    store = FakeStore([outbox_event(index) for index in range(1, 6)])
    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), lambda _event: None)],
        OutboxWorkerOptions(batch_size=5, concurrency=2),
    )

    assert worker.run_once() == OutboxRunResult(claimed=5, delivered=5, failed=0, claim_lost=0)
    assert store.claim_limits == [2, 2, 1]


def test_worker_requires_claim_renewal_support_at_construction() -> None:
    store = FakeStore([])
    store.renew = None  # type: ignore[method-assign,assignment]
    with pytest.raises(TypeError, match="claim renewal"):
        OutboxWorker(store, [FakeHandler(("usage.charge_recorded",), lambda _event: None)])  # type: ignore[arg-type]


def test_attempt_limit_matches_postgres_maximum() -> None:
    handler = FakeHandler(("usage.charge_recorded",), lambda _event: None)
    with pytest.raises(ValueError, match="attempt_limit"):
        OutboxWorker(FakeStore([]), [handler], OutboxWorkerOptions(attempt_limit=101))

    worker = OutboxWorker(FakeStore([]), [handler], OutboxWorkerOptions(attempt_limit=100))
    assert worker.run_once() == OutboxRunResult(claimed=0, delivered=0, failed=0, claim_lost=0)


def test_worker_renews_active_handler_claim_and_stops_heartbeat() -> None:
    release_handler = threading.Event()
    renewed = threading.Event()
    store = FakeStore([outbox_event(1)])
    store.on_renew = renewed.set

    def blocked_handler(_event: OutboxEvent) -> None:
        if not release_handler.wait(2):
            raise TimeoutError("test handler was not released")

    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), blocked_handler)],
        OutboxWorkerOptions(lease_seconds=1),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(worker.run_once)
        assert renewed.wait(2)
        release_handler.set()
        assert result.result(timeout=2) == OutboxRunResult(claimed=1, delivered=1, failed=0, claim_lost=0)

    renewals_after_stop = len(store.renewed)
    renewed.clear()
    assert not renewed.wait(0.5)
    assert len(store.renewed) == renewals_after_stop


def test_complete_false_surfaces_claim_loss_without_failure_write() -> None:
    store = FakeStore([outbox_event(1, attempt_count=3)])
    store.complete_result = False
    outcomes: list[OutboxEventOutcome] = []
    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), lambda _event: None)],
        OutboxWorkerOptions(on_event_outcome=outcomes.append),
    )

    assert worker.run_once() == OutboxRunResult(claimed=1, delivered=0, failed=1, claim_lost=1)
    assert store.failed == []
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == "claim_lost"
    assert outcome.summary == "outbox_claim_lost:UnknownError"
    assert outcome.claim_loss_phase == "complete"
    assert outcome.topic == "usage.charge_recorded"
    assert outcome.attempt_count == 3
    assert "event" not in outcome.model_fields_set
    assert "payload" not in outcome.model_fields_set
    assert "claim_token" not in outcome.model_fields_set


def test_failure_summary_is_safe_and_fail_false_is_claim_loss() -> None:
    store = FakeStore([outbox_event(1, attempt_count=2)])
    store.fail_result = False
    outcomes: list[OutboxEventOutcome] = []

    def failing_handler(_event: OutboxEvent) -> None:
        raise RuntimeError("secret=https://sink.invalid/token/abc")

    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), failing_handler)],
        OutboxWorkerOptions(on_event_outcome=outcomes.append),
    )

    assert worker.run_once().claim_lost == 1
    assert store.failed[0][1] == "outbox_delivery_failed:RuntimeError"
    assert "secret" not in store.failed[0][1]
    assert outcomes[0].status == "claim_lost"
    assert outcomes[0].summary == "outbox_delivery_failed:RuntimeError"
    assert outcomes[0].claim_loss_phase == "fail"


def test_throwing_outcome_callback_does_not_stop_delivery() -> None:
    store = FakeStore([outbox_event(1), outbox_event(2)])
    callback = Mock(side_effect=RuntimeError("observer failed"))
    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), lambda _event: None)],
        OutboxWorkerOptions(batch_size=2, concurrency=2, on_event_outcome=callback),
    )

    assert worker.run_once() == OutboxRunResult(claimed=2, delivered=2, failed=0, claim_lost=0)
    assert callback.call_count == 2


def test_repository_rejects_raw_failure_text() -> None:
    query = Mock(return_value=[{"fail_tenant_outbox_event": True}])
    repository = PostgresStorageRepository(query, TENANT_ID)

    with pytest.raises(ValueError, match="summary"):
        repository.fail(outbox_event(1), "secret sink response", 30, 10)
    query.assert_not_called()

    assert repository.fail(outbox_event(1), "outbox_delivery_failed:RuntimeError", 30, 10)


def test_repository_rejects_malformed_boolean_rpc_envelopes() -> None:
    repository = PostgresStorageRepository(Mock(return_value=[]), TENANT_ID)
    with pytest.raises(RuntimeError, match="expected one"):
        repository.complete(outbox_event(1))

    repository = PostgresStorageRepository(Mock(return_value=[{"complete": "true"}]), TENANT_ID)
    with pytest.raises(RuntimeError, match="must be a boolean"):
        repository.complete(outbox_event(1))


def test_repository_rejects_non_object_outbox_payloads() -> None:
    query = Mock(
        return_value=[
            {
                "event_id": "1",
                "tenant_id": TENANT_ID,
                "topic": "usage.charge_recorded",
                "aggregate_type": "credit_usage_charge",
                "aggregate_id": "00000000-0000-0000-0000-000000000001",
                "payload_version": 1,
                "payload": "{}",
                "claim_token": "10000000-0000-0000-0000-000000000001",
                "attempt_count": 1,
                "created_at": "2026-08-10T00:00:00.000Z",
            }
        ]
    )
    repository = PostgresStorageRepository(query, TENANT_ID)

    with pytest.raises(RuntimeError, match="payload must be a JSON object"):
        repository.claim(("usage.charge_recorded",), 1, 60)


def test_repository_maps_stats_and_dead_letter_cursor_page() -> None:
    query = Mock(
        side_effect=[
            [
                {
                    "pending_count": "2",
                    "processing_count": "1",
                    "delivered_count": "3",
                    "dead_letter_count": "2",
                    "oldest_pending_at": "2026-08-10T00:00:00.000Z",
                }
            ],
            [
                {
                    "event_id": "10",
                    "tenant_id": TENANT_ID,
                    "topic": "usage.charge_recorded",
                    "aggregate_type": "credit_usage_charge",
                    "aggregate_id": "00000000-0000-0000-0000-000000000010",
                    "payload_version": 1,
                    "attempt_count": 10,
                    "last_error": "outbox_delivery_failed:RuntimeError",
                    "created_at": "2026-08-10T00:00:00.000Z",
                    "updated_at": "2026-08-10T00:01:00.000Z",
                },
                {
                    "event_id": "11",
                    "tenant_id": TENANT_ID,
                    "topic": "usage.charge_recorded",
                    "aggregate_type": "credit_usage_charge",
                    "aggregate_id": "00000000-0000-0000-0000-000000000011",
                    "payload_version": 1,
                    "attempt_count": 10,
                    "last_error": "outbox_delivery_failed:RuntimeError",
                    "created_at": "2026-08-10T00:02:00.000Z",
                    "updated_at": "2026-08-10T00:03:00.000Z",
                },
            ],
        ]
    )
    repository = PostgresStorageRepository(query, TENANT_ID)

    stats = repository.stats()
    assert stats.pending_count == 2
    assert stats.dead_letter_count == 2
    page = repository.list_dead_letters(OutboxDeadLetterListOptions(limit=1))
    assert [item.event_id for item in page.items] == ["10"]
    assert page.next_cursor is not None
    assert page.next_cursor.event_id == "10"


def test_repository_rejects_cross_tenant_event_capability() -> None:
    repository = PostgresStorageRepository(Mock(), TENANT_ID)
    other = outbox_event(1).model_copy(update={"tenant_id": "00000000-0000-0000-0000-000000000002"})

    with pytest.raises(RuntimeError, match="tenant"):
        repository.renew(other, 60)
