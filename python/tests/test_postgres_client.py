from typing import Any, cast

import pytest

from bursar.shared.postgres_client import PostgresClient, create_pool


class FakePool:
    def __init__(self) -> None:
        self.close_calls = 0

    def closeall(self) -> None:
        self.close_calls += 1

    def getconn(self) -> Any:
        raise AssertionError("query was not expected")

    def putconn(self, _conn: Any, close: bool = False) -> None:
        return None


def test_borrowed_pool_is_not_closed_but_client_cannot_be_reused() -> None:
    pool = FakePool()
    client = PostgresClient.from_pool(cast(Any, pool))

    client.close()
    client.close()

    assert pool.close_calls == 0
    with pytest.raises(RuntimeError, match="has been closed"):
        client.query("select 1")


def test_owned_pool_is_closed_exactly_once() -> None:
    pool = FakePool()
    client = object.__new__(PostgresClient)
    client._pool = cast(Any, pool)
    client._owns_pool = True
    client._closed = False

    client.close()
    client.close()

    assert pool.close_calls == 1


def test_owned_pool_is_created_lazily_and_closed_once(monkeypatch) -> None:
    connection = object()
    calls: list[tuple] = []

    class UnderlyingPool:
        def getconn(self):
            return connection

        def putconn(self, conn, key=None, close=False) -> None:
            calls.append(("put", conn, key, close))

        def closeall(self) -> None:
            calls.append(("close",))

    def create(*args, **kwargs):
        calls.append(("create", args, kwargs))
        return UnderlyingPool()

    monkeypatch.setattr("bursar.shared.postgres_client.psycopg2.pool.ThreadedConnectionPool", create)
    pool = create_pool("postgresql://database.test/bursar")

    assert calls == []
    assert pool.getconn() is connection
    pool.putconn(connection)
    pool.closeall()
    pool.closeall()

    assert [call[0] for call in calls] == ["create", "put", "close"]


def test_closing_unused_owned_pool_does_not_open_a_connection(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("pool constructor was not expected")

    monkeypatch.setattr(
        "bursar.shared.postgres_client.psycopg2.pool.ThreadedConnectionPool",
        unexpected,
    )
    pool = create_pool("postgresql://database.test/bursar")

    pool.closeall()
