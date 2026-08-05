"""Leased outbox dispatcher mirroring JavaScript ``storage/outbox-worker.ts``."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor

from pydantic import BaseModel, ConfigDict

from bursar.shared.diagnostics import bounded_diagnostic_message
from bursar.storage.ports import OutboxEvent, OutboxHandler, OutboxStore


class _OutboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutboxWorkerOptions(_OutboxModel):
    batch_size: int = 100
    concurrency: int = 4
    lease_seconds: int = 60
    poll_interval_ms: int = 1_000
    retry_delay_seconds: int = 30
    max_retry_delay_seconds: int = 3_600
    attempt_limit: int = 10
    on_error: Callable[[BaseException], None] | None = None


class OutboxRunResult(_OutboxModel):
    claimed: int
    delivered: int
    failed: int


def _integer_option(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        msg = f"{name} must be an integer between {minimum} and {maximum}"
        raise ValueError(msg)
    return value


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
        self._validate_options(self._options)
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
        events = self._store.claim(
            self._topics,
            self._options.batch_size,
            self._options.lease_seconds,
        )
        if not events:
            return OutboxRunResult(claimed=0, delivered=0, failed=0)
        worker_count = min(self._options.concurrency, len(events))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            delivered = list(executor.map(self._dispatch_event, events))
        delivered_count = sum(delivered)
        return OutboxRunResult(
            claimed=len(events),
            delivered=delivered_count,
            failed=len(events) - delivered_count,
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except BaseException as error:
                if self._options.on_error is not None:
                    self._options.on_error(error)
            self._stop_event.wait(self._options.poll_interval_ms / 1_000)

    def _dispatch_event(self, event: OutboxEvent) -> bool:
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
            if not self._store.complete(event):
                msg = f"Lost outbox claim for event {event.event_id}"
                raise RuntimeError(msg)
            return True
        except BaseException as error:
            message = bounded_diagnostic_message(
                f"{type(error).__name__}: {error}",
                "outbox_delivery_failed",
            )
            exponential_delay = self._options.retry_delay_seconds * 2 ** max(event.attempt_count - 1, 0)
            retry_delay = min(exponential_delay, self._options.max_retry_delay_seconds)
            self._store.fail(
                event,
                message,
                retry_delay,
                self._options.attempt_limit,
            )
            return False

    @staticmethod
    def _validate_options(options: OutboxWorkerOptions) -> None:
        _integer_option(options.batch_size, "batch_size", 1, 1_000)
        _integer_option(options.concurrency, "concurrency", 1, 100)
        _integer_option(options.lease_seconds, "lease_seconds", 1, 3_600)
        _integer_option(options.poll_interval_ms, "poll_interval_ms", 10, 3_600_000)
        _integer_option(options.retry_delay_seconds, "retry_delay_seconds", 1, 86_400)
        _integer_option(options.max_retry_delay_seconds, "max_retry_delay_seconds", 1, 86_400)
        _integer_option(options.attempt_limit, "attempt_limit", 1, 1_000)
        if options.max_retry_delay_seconds < options.retry_delay_seconds:
            msg = "max_retry_delay_seconds must be at least retry_delay_seconds"
            raise ValueError(msg)
