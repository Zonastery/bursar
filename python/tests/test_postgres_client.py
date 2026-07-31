from typing import Any, cast

import pytest

from bursar.shared.postgres_client import PostgresClient


class FakePool:
    def __init__(self) -> None:
        self.close_calls = 0

    def closeall(self) -> None:
        self.close_calls += 1


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
