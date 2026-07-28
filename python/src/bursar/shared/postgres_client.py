"""Postgres client lifecycle — mirrors JS SDK's ``shared/postgres-client.ts``.

Manages a connection pool and provides a query function.
"""

from __future__ import annotations

from typing import Any


def create_pool(dsn: str, min_connections: int = 1, max_connections: int = 10) -> Any:
    """Create a psycopg2 connection pool.

    Args:
        dsn: PostgreSQL connection string.
        min_connections: Minimum connections in the pool.
        max_connections: Maximum connections in the pool.

    Returns:
        A psycopg2 ``ThreadedConnectionPool``.
    """
    import psycopg2.pool

    return psycopg2.pool.ThreadedConnectionPool(min_connections, max_connections, dsn)


def close_pool(pool: Any) -> None:
    """Close all connections in a pool."""
    if pool is not None:
        pool.closeall()
