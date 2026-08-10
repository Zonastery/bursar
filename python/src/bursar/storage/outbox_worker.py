"""Leased outbox dispatcher mirroring JavaScript ``storage/outbox-worker.ts``."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bursar.shared.diagnostics import persisted_diagnostic_summary
from bursar.storage.ports import OutboxEvent, OutboxHandler, OutboxStore


class _OutboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


OutboxEventOutcomeStatus = Literal["delivered", "delivery_failed", "claim_lost"]
OutboxClaimLossPhase = Literal["heartbeat", "complete", "fail"]


class OutboxEventOutcome(_OutboxModel):
    topic: str
    attempt_count: int
    status: OutboxEventOutcomeStatus
    # Persistence-safe diagnostic summary; never a raw exception message.
    summary: str | None
    duration_ms: float = Field(ge=0)
    retry_delay_seconds: int | None
    dead_lettered: bool
    claim_loss_phase: OutboxClaimLossPhase | None


class OutboxWorkerOptions(_OutboxModel):
    batch_size: int = Field(default=100, strict=True, ge=1, le=1_000)
    concurrency: int = Field(default=4, strict=True, ge=1, le=100)
    lease_seconds: int = Field(default=60, strict=True, ge=1, le=3_600)
    poll_interval_ms: int = Field(default=1_000, strict=True, ge=10, le=3_600_000)
    retry_delay_seconds: int = Field(default=30, strict=True, ge=1, le=86_400)
    max_retry_delay_seconds: int = Field(default=3_600, strict=True, ge=1, le=86_400)
    attempt_limit: int = Field(default=10, strict=True, ge=1, le=100)
    on_error: Callable[[BaseException], None] | None = None
    on_event_outcome: Callable[[OutboxEventOutcome], None] | None = None

    @model_validator(mode="after")
    def validate_retry_delays(self) -> OutboxWorkerOptions:
        if self.max_retry_delay_seconds < self.retry_delay_seconds:
            raise ValueError("max_retry_delay_seconds must be at least retry_delay_seconds")
        return self


class OutboxRunResult(_OutboxModel):
    claimed: int
    delivered: int
    failed: int
    claim_lost: int = Field(ge=0)


class _ClaimHeartbeat:
    def __init__(self, store: OutboxStore, event: OutboxEvent, lease_seconds: int) -> None:
        self._store = store
        self._event = event
        self._lease_seconds = lease_seconds
        self._interval_seconds = max(0.1, lease_seconds / 3)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._claim_lost = False
        self._summary: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"bursar-outbox-heartbeat-{event.event_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> tuple[bool, str | None]:
        self._stop_event.set()
        if self._thread is not threading.current_thread():
            self._thread.join()
        with self._state_lock:
            return self._claim_lost, self._summary

    def _lose_claim(self, error: object | None) -> None:
        with self._state_lock:
            self._claim_lost = True
            self._summary = persisted_diagnostic_summary(error, "outbox_claim_lost")
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                renewed = self._store.renew(self._event, self._lease_seconds)
            except BaseException as error:
                self._lose_claim(error)
                return
            if not renewed:
                self._lose_claim(None)
                return


class OutboxWorker:
    """Generic leased-outbox dispatcher.

    Handlers must be idempotent because a process can stop after the external
    write succeeds but before PostgreSQL records the acknowledgement.
    """

    def __init__(
        self,
        store: OutboxStore,
        handlers: Sequence[OutboxHandler],
        options: OutboxWorkerOptions | None = None,
    ) -> None:
        if not handlers:
            msg = "OutboxWorker requires at least one handler"
            raise ValueError(msg)
        if not callable(getattr(store, "renew", None)):
            msg = "OutboxWorker store must support claim renewal"
            raise TypeError(msg)
        self._store = store
        self._handlers: dict[str, list[OutboxHandler]] = {}
        for handler in handlers:
            if not handler.topics:
                msg = "Outbox handlers must declare at least one topic"
                raise ValueError(msg)
            for topic in handler.topics:
                if not topic.strip():
                    msg = "Outbox topics must not be empty"
                    raise ValueError(msg)
                self._handlers.setdefault(topic, []).append(handler)
        self._topics = sorted(self._handlers)
        self._options = options or OutboxWorkerOptions()
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._active_future: Future[OutboxRunResult] | None = None
        self._active_thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False

    def start(self) -> None:
        if self._started:
            return
        if self._stopped:
            msg = "OutboxWorker cannot be restarted after stop"
            raise RuntimeError(msg)
        self._started = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="bursar-outbox-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None
        with self._run_lock:
            active_future = self._active_future
            active_thread_id = self._active_thread_id
        if active_future is not None and active_thread_id != threading.get_ident():
            active_future.result()

    def run_once(self) -> OutboxRunResult:
        if self._stopped:
            msg = "OutboxWorker has been stopped"
            raise RuntimeError(msg)
        with self._run_lock:
            future = self._active_future
            owns_run = future is None
            if future is None:
                future = Future()
                self._active_future = future
            if not owns_run:
                return future.result()
            self._active_thread_id = threading.get_ident()

        try:
            result = self._dispatch_once()
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            with self._run_lock:
                if self._active_future is future:
                    self._active_future = None
                    self._active_thread_id = None

    def _dispatch_once(self) -> OutboxRunResult:
        claimed_count = 0
        delivered_count = 0
        failed_count = 0
        claim_lost_count = 0
        remaining_budget = self._options.batch_size

        while remaining_budget > 0:
            available_slots = min(self._options.concurrency, remaining_budget)
            events = self._store.claim(
                self._topics,
                available_slots,
                self._options.lease_seconds,
            )
            if not events:
                break
            if len(events) > available_slots:
                msg = f"Outbox store returned {len(events)} events for {available_slots} available slots"
                raise RuntimeError(msg)

            claimed_count += len(events)
            remaining_budget -= len(events)
            with ThreadPoolExecutor(max_workers=len(events)) as executor:
                outcomes = list(executor.map(self._dispatch_event, events))
            for outcome in outcomes:
                if outcome == "delivered":
                    delivered_count += 1
                else:
                    failed_count += 1
                    if outcome == "claim_lost":
                        claim_lost_count += 1
            if len(events) < available_slots:
                break

        return OutboxRunResult(
            claimed=claimed_count,
            delivered=delivered_count,
            failed=failed_count,
            claim_lost=claim_lost_count,
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except BaseException as error:
                if self._options.on_error is not None:
                    with suppress(BaseException):
                        self._options.on_error(error)
            self._stop_event.wait(self._options.poll_interval_ms / 1_000)

    def _report_outcome(self, outcome: OutboxEventOutcome) -> None:
        if self._options.on_event_outcome is not None:
            with suppress(BaseException):
                self._options.on_event_outcome(outcome)

    def _dispatch_event(self, event: OutboxEvent) -> OutboxEventOutcomeStatus:
        started_at = perf_counter()
        heartbeat = _ClaimHeartbeat(self._store, event, self._options.lease_seconds)
        heartbeat.start()
        delivery_error: BaseException | None = None
        try:
            handlers = self._handlers.get(event.topic)
            if not handlers:
                msg = f"No handler for outbox topic {event.topic}"
                raise RuntimeError(msg)
            if len(handlers) == 1:
                handlers[0].handle(event)
            else:
                with ThreadPoolExecutor(max_workers=len(handlers)) as executor:
                    list(executor.map(lambda handler: handler.handle(event), handlers))
        except BaseException as error:
            delivery_error = error

        heartbeat_lost, heartbeat_summary = heartbeat.stop()
        if heartbeat_lost:
            return self._claim_lost(event, "heartbeat", heartbeat_summary, started_at)
        if delivery_error is not None:
            return self._fail_delivery(event, delivery_error, started_at)

        try:
            if not self._store.complete(event):
                return self._claim_lost(
                    event,
                    "complete",
                    persisted_diagnostic_summary(None, "outbox_claim_lost"),
                    started_at,
                )
        except BaseException as error:
            return self._claim_lost(
                event,
                "complete",
                persisted_diagnostic_summary(error, "outbox_claim_lost"),
                started_at,
            )

        self._report_outcome(
            OutboxEventOutcome(
                topic=event.topic,
                attempt_count=event.attempt_count,
                status="delivered",
                summary=None,
                duration_ms=max((perf_counter() - started_at) * 1_000, 0.0),
                retry_delay_seconds=None,
                dead_lettered=False,
                claim_loss_phase=None,
            )
        )
        return "delivered"

    def _fail_delivery(
        self,
        event: OutboxEvent,
        error: BaseException,
        started_at: float,
    ) -> OutboxEventOutcomeStatus:
        summary = persisted_diagnostic_summary(error, "outbox_delivery_failed")
        exponential_delay = self._options.retry_delay_seconds * 2 ** max(event.attempt_count - 1, 0)
        retry_delay = min(exponential_delay, self._options.max_retry_delay_seconds)
        try:
            if not self._store.fail(
                event,
                summary,
                retry_delay,
                self._options.attempt_limit,
            ):
                return self._claim_lost(event, "fail", summary, started_at)
        except BaseException as failure_error:
            return self._claim_lost(
                event,
                "fail",
                persisted_diagnostic_summary(failure_error, "outbox_claim_lost"),
                started_at,
            )

        self._report_outcome(
            OutboxEventOutcome(
                topic=event.topic,
                attempt_count=event.attempt_count,
                status="delivery_failed",
                summary=summary,
                duration_ms=max((perf_counter() - started_at) * 1_000, 0.0),
                retry_delay_seconds=retry_delay,
                dead_lettered=event.attempt_count >= self._options.attempt_limit,
                claim_loss_phase=None,
            )
        )
        return "delivery_failed"

    def _claim_lost(
        self,
        event: OutboxEvent,
        phase: OutboxClaimLossPhase,
        summary: str | None,
        started_at: float,
    ) -> OutboxEventOutcomeStatus:
        self._report_outcome(
            OutboxEventOutcome(
                topic=event.topic,
                attempt_count=event.attempt_count,
                status="claim_lost",
                summary=summary or persisted_diagnostic_summary(None, "outbox_claim_lost"),
                duration_ms=max((perf_counter() - started_at) * 1_000, 0.0),
                retry_delay_seconds=None,
                dead_lettered=False,
                claim_loss_phase=phase,
            )
        )
        return "claim_lost"
