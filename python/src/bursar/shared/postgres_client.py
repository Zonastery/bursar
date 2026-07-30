"""Postgres client lifecycle — mirrors JS SDK's ``shared/postgres-client.ts``.

Manages a connection pool and provides a query function.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool


class PostgresClient:
    """Thin wrapper around psycopg2 connection pool matching JS PostgresClient API."""

    def __init__(self, dsn: str, min_connections: int = 1, max_connections: int = 10):
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = psycopg2.pool.ThreadedConnectionPool(
            min_connections, max_connections, dsn
        )
        self._owns_pool = True
        self._closed = False

    @classmethod
    def from_pool(cls, pool: psycopg2.pool.ThreadedConnectionPool) -> PostgresClient:
        """Create client from existing pool (borrowed mode)."""
        instance = cls.__new__(cls)
        instance._pool = pool
        instance._owns_pool = False
        instance._closed = False
        return instance

    def query(self, text: str, params: list | None = None) -> list[dict]:
        if self._closed or self._pool is None:
            raise RuntimeError("PostgreSQL client has been closed")
        conn = self._pool.getconn()
        try:
            # A pooled connection must never be returned with an open or failed
            # transaction. The connection context commits successful writes and
            # rolls back exceptions before putconn makes it available again.
            with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(text, params or [])
                if cur.description:
                    return [dict(row) for row in cur.fetchall()]
                return []
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool = self._pool
        self._pool = None
        if self._owns_pool and pool is not None:
            pool.closeall()


def create_pool(dsn: str, min_connections: int = 1, max_connections: int = 10) -> Any:
    """Create a psycopg2 connection pool.

    Args:
        dsn: PostgreSQL connection string.
        min_connections: Minimum connections in the pool.
        max_connections: Maximum connections in the pool.

    Returns:
        A psycopg2 ``ThreadedConnectionPool``.
    """
    return psycopg2.pool.ThreadedConnectionPool(min_connections, max_connections, dsn)


def close_pool(pool: Any) -> None:
    """Close all connections in a pool."""
    if pool is not None:
        pool.closeall()
