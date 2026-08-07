from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from bursar.errors import (
    StoreError,
    StoreTimeoutError,
    StoreUnavailableError,
    is_retryable_bursar_error,
)
from bursar.retry import BursarRetryOptions, retry_bursar_operation, retry_bursar_operation_async
from bursar.shared.postgres_client import PostgresClient, PostgresConnectionOptions
from bursar.shared.postgres_errors import normalize_postgres_error
from bursar.storage.runtime import BursarRuntimeOptions, create_bursar_runtime

TENANT_ID = "00000000-0000-0000-0000-000000000001"


class DriverError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.pgcode = code


class FakeCursor:
    def __init__(self, *, query_error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.description: list[object] | None = [{"ok": True}]
        self.query_error = query_error

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, text: str, params: list[Any] | None = None) -> None:
        self.calls.append((text, params or []))
        if text == "SELECT FAIL" and self.query_error is not None:
            raise self.query_error
        if text.startswith("SET LOCAL"):
            self.description = None
        elif not text.startswith("SELECT set_config"):
            self.description = [{"ok": True}]

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"ok": True}]

    def callproc(self, _name: str, _params: list[Any]) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor, *, rollback_error: BaseException | None = None) -> None:
        self.cursor_value = cursor
        self.rollback_error = rollback_error
        self.autocommit = False
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self, **_kwargs: Any) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_error is not None:
            raise self.rollback_error


class FakePool:
    def __init__(self, connection: FakeConnection, *, release_error: BaseException | None = None) -> None:
        self.connection = connection
        self.release_error = release_error
        self.put_calls: list[tuple[FakeConnection, bool]] = []
        self.close_calls = 0

    def getconn(self) -> FakeConnection:
        return self.connection

    def putconn(
        self,
        conn: FakeConnection | None = None,
        key: object | None = None,
        close: bool = False,
    ) -> None:
        del key
        if conn is not None:
            self.put_calls.append((conn, close))
        if self.release_error is not None:
            raise self.release_error

    def closeall(self) -> None:
        self.close_calls += 1


def test_retry_uses_tenacity_backoff_hooks_and_fail_closed_classification() -> None:
    attempts = 0
    retries: list[tuple[str, int, float]] = []

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StoreUnavailableError("temporary")
        return "ok"

    assert (
        retry_bursar_operation(
            operation,
            retry_options=BursarRetryOptions(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
                on_retry=lambda error, attempt, delay: retries.append((str(error), attempt, delay)),
            ),
        )
        == "ok"
    )
    assert attempts == 2
    assert retries == [("temporary", 2, 0)]
    assert is_retryable_bursar_error(StoreError("permanent")) is False


@pytest.mark.asyncio
async def test_async_retry_is_cancellable_by_task_cancellation() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise StoreUnavailableError("temporary")

    task = asyncio.create_task(
        retry_bursar_operation_async(
            operation,
            retry_options=BursarRetryOptions(
                max_attempts=10,
                base_delay_seconds=1,
                max_delay_seconds=1,
                jitter=False,
            ),
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 1


def test_postgres_client_configures_deadlines_and_tenant_scope() -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    pool = FakePool(connection)
    client = PostgresClient.from_pool(
        pool,
        tenant_id=TENANT_ID,
        statement_timeout_ms=1_234,
        idle_transaction_timeout_ms=5_678,
    )

    assert client.query("SELECT 1") == [{"ok": True}]
    assert cursor.calls[0][0] == "SET LOCAL ROLE bursar_client"
    configuration = cursor.calls[1]
    assert "statement_timeout" in configuration[0]
    assert configuration[1] == [TENANT_ID, "postgres", "postgres", "1234", "5678"]
    assert cursor.calls[2][0] == "SET LOCAL search_path TO bursar, public"
    assert pool.put_calls == [(connection, False)]


def test_postgres_client_preserves_primary_failure_and_discards_poisoned_connection() -> None:
    primary = DriverError("socket reset", "ECONNRESET")
    rollback = DriverError("connection lost", "ECONNRESET")
    cursor = FakeCursor(query_error=primary)
    connection = FakeConnection(cursor, rollback_error=rollback)
    pool = FakePool(connection)
    client = PostgresClient.from_pool(pool, tenant_id=TENANT_ID)

    with pytest.raises(StoreUnavailableError) as failure:
        client.query("SELECT FAIL")

    assert failure.value.cause is primary
    assert failure.value.indeterminate is True
    assert failure.value.details is not None
    assert failure.value.details["rollback_failed"] is True
    assert pool.put_calls == [(connection, True)]


def test_postgres_client_release_observer_cannot_turn_success_into_failure() -> None:
    release_error = DriverError("socket reset", "ECONNRESET")
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    pool = FakePool(connection, release_error=release_error)
    observed: list[BaseException] = []
    client = PostgresClient.from_pool(pool, on_pool_error=observed.append)

    assert client.query("SELECT 1") == [{"ok": True}]
    assert len(observed) == 1
    assert isinstance(observed[0], StoreUnavailableError)


def test_postgres_options_validate_and_classify_timeouts() -> None:
    assert PostgresConnectionOptions().statement_timeout_ms == 30_000
    with pytest.raises(ValueError, match="max_connections"):
        PostgresConnectionOptions(max_connections=0)
    with pytest.raises(ValueError, match="access_role"):
        PostgresClient.from_pool(FakePool(FakeConnection(FakeCursor())), access_role="owner")  # type: ignore[arg-type]

    timeout = DriverError("canceling statement due to statement timeout", "57014")
    client = PostgresClient.from_pool(FakePool(FakeConnection(FakeCursor(query_error=timeout))), tenant_id=TENANT_ID)
    with pytest.raises(StoreTimeoutError):
        client.query("SELECT FAIL")


@pytest.mark.parametrize(
    ("error", "expected"),
    [(ConnectionRefusedError("refused"), StoreUnavailableError), (TimeoutError("timed out"), StoreTimeoutError)],
)
def test_transport_failures_without_driver_codes_are_classified(
    error: BaseException,
    expected: type[StoreError],
) -> None:
    normalized = normalize_postgres_error(error, operation="query", phase="query", indeterminate=True)
    assert type(normalized) is expected
    assert isinstance(normalized, StoreError)
    assert normalized.indeterminate is True


def test_runtime_close_attempts_every_resource_and_replays_failure() -> None:
    pool = FakePool(FakeConnection(FakeCursor()))
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pool,
            tenant_id=UUID(TENANT_ID),
        )
    )
    runtime._owns_pool = True
    credit_failure = RuntimeError("credit close failed")
    runtime.credit_store.close = Mock(side_effect=credit_failure)
    runtime.billing_store.close = Mock()
    pool.closeall = Mock()

    with pytest.raises(RuntimeError, match="credit close failed") as first:
        runtime.close()
    runtime.billing_store.close.assert_called_once()
    pool.closeall.assert_called_once()

    with pytest.raises(RuntimeError) as second:
        runtime.close()
    assert second.value is first.value


def test_postgres_options_json_schema_handles_observer_callback() -> None:
    schema = TypeAdapter(PostgresConnectionOptions).json_schema()
    assert "on_pool_error" not in schema["properties"]
