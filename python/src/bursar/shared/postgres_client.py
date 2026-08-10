"""Resilient PostgreSQL connection-pool boundary for the Python SDK."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil, isfinite
from threading import RLock
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import psycopg2.pool
from pydantic.json_schema import SkipJsonSchema

from bursar.errors import BursarError, StoreClosedError
from bursar.providers.types import ProviderEnvironment
from bursar.shared.postgres_errors import normalize_postgres_error

PostgresAccessRole = Literal["bursar_client", "bursar_operator"]

# Register only psycopg2's official write adapter. Registering the full UUID
# codec also changes every UUID read into a stdlib UUID object, which would be a
# breaking change to Bursar's established string-valued repository contract.
psycopg2.extensions.register_adapter(UUID, psycopg2.extras.UUID_adapter)


class PostgresPool(Protocol):
    def getconn(self) -> Any: ...

    def putconn(self, conn: Any = None, key: Any = None, close: bool = False) -> None: ...

    def closeall(self) -> None: ...


class _LazyPostgresPool:
    """Create the psycopg2 pool on first checkout and close it exactly once."""

    def __init__(self, factory: Callable[[], PostgresPool]) -> None:
        self._factory = factory
        self._pool: PostgresPool | None = None
        self._closed = False
        self._lock = RLock()

    def _get_pool(self) -> PostgresPool:
        with self._lock:
            if self._closed:
                raise StoreClosedError("PostgreSQL pool has been closed")
            if self._pool is None:
                self._pool = self._factory()
            return self._pool

    def getconn(self) -> Any:
        return self._get_pool().getconn()

    def putconn(self, conn: Any = None, key: Any = None, close: bool = False) -> None:
        with self._lock:
            pool = self._pool
        if pool is None:
            raise StoreClosedError("PostgreSQL pool has no checked-out connection")
        pool.putconn(conn, key, close)

    def closeall(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pool = self._pool
            self._pool = None
        if pool is not None:
            pool.closeall()


@dataclass(frozen=True, slots=True)
class PostgresConnectionOptions:
    """Connection, transaction-deadline, and observability controls."""

    connection_timeout_seconds: float = 10.0
    statement_timeout_ms: int = 30_000
    idle_transaction_timeout_ms: int = 30_000
    max_connections: int = 10
    application_name: str = "bursar-python"
    on_pool_error: SkipJsonSchema[Callable[[BursarError], None]] | None = None

    def __post_init__(self) -> None:
        _validate_timeout(self.connection_timeout_seconds, "connection_timeout_seconds", allow_float=True)
        _validate_timeout(self.statement_timeout_ms, "statement_timeout_ms")
        _validate_timeout(self.idle_transaction_timeout_ms, "idle_transaction_timeout_ms")
        if (
            not isinstance(self.max_connections, int)
            or isinstance(self.max_connections, bool)
            or self.max_connections < 1
        ):
            raise ValueError("max_connections must be a positive integer")
        if not isinstance(self.application_name, str) or not self.application_name.strip():
            raise ValueError("application_name must not be empty")
        if "\x00" in self.application_name:
            raise ValueError("application_name must not contain null bytes")
        if self.on_pool_error is not None and not callable(self.on_pool_error):
            raise TypeError("on_pool_error must be callable")


def _validate_timeout(value: float, name: str, *, allow_float: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    if not allow_float and not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")


def _validate_pool(pool: object) -> PostgresPool:
    if not all(callable(getattr(pool, method, None)) for method in ("getconn", "putconn", "closeall")):
        raise TypeError("postgres pool must provide getconn(), putconn(), and closeall() methods")
    return cast(PostgresPool, pool)


class PostgresClient:
    """Own or borrow a psycopg2 pool and normalize every transaction failure."""

    def __init__(
        self,
        dsn: str,
        min_connections: int = 1,
        max_connections: int = 10,
        *,
        tenant_id: str | UUID | None = None,
        access_role: PostgresAccessRole | None = None,
        usage_backend: Literal["postgres", "clickhouse"] = "postgres",
        billing_payload_backend: Literal["postgres", "s3"] = "postgres",
        provider_environment: ProviderEnvironment = "live",
        connection_timeout_seconds: float = 10.0,
        statement_timeout_ms: int = 30_000,
        idle_transaction_timeout_ms: int = 30_000,
        application_name: str = "bursar-python",
        on_pool_error: Callable[[BursarError], None] | None = None,
        postgres_options: PostgresConnectionOptions | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("postgres connection string must not be empty")
        if not isinstance(min_connections, int) or isinstance(min_connections, bool) or min_connections < 1:
            raise ValueError("min_connections must be a positive integer")
        if (
            not isinstance(max_connections, int)
            or isinstance(max_connections, bool)
            or max_connections < min_connections
        ):
            raise ValueError("max_connections must be at least min_connections")
        self._options = postgres_options or PostgresConnectionOptions(
            connection_timeout_seconds=connection_timeout_seconds,
            statement_timeout_ms=statement_timeout_ms,
            idle_transaction_timeout_ms=idle_transaction_timeout_ms,
            application_name=application_name,
            on_pool_error=on_pool_error,
        )
        self._tenant_id = _normalize_tenant_id(tenant_id) if tenant_id is not None else None
        self._access_role = _normalize_access_role(access_role, self._tenant_id)
        self._usage_backend = _normalize_backend(usage_backend, ("postgres", "clickhouse"), "usage_backend")
        self._billing_payload_backend = _normalize_backend(
            billing_payload_backend,
            ("postgres", "s3"),
            "billing_payload_backend",
        )
        self._provider_environment = _normalize_provider_environment(provider_environment)
        effective_max_connections = self._options.max_connections if postgres_options is not None else max_connections
        if effective_max_connections < min_connections:
            raise ValueError("max_connections must be at least min_connections")
        self._pool: PostgresPool | None = create_pool(
            dsn,
            min_connections=min_connections,
            max_connections=effective_max_connections,
            postgres_options=self._options,
        )
        self._owns_pool = True
        self._closed = False

    @classmethod
    def from_pool(
        cls,
        pool: PostgresPool,
        *,
        tenant_id: str | UUID | None = None,
        access_role: PostgresAccessRole | None = None,
        usage_backend: Literal["postgres", "clickhouse"] = "postgres",
        billing_payload_backend: Literal["postgres", "s3"] = "postgres",
        provider_environment: ProviderEnvironment = "live",
        connection_timeout_seconds: float = 10.0,
        statement_timeout_ms: int = 30_000,
        idle_transaction_timeout_ms: int = 30_000,
        application_name: str = "bursar-python",
        on_pool_error: Callable[[BursarError], None] | None = None,
        postgres_options: PostgresConnectionOptions | None = None,
    ) -> PostgresClient:
        """Create a client around a borrowed pool; ``close`` never ends it."""

        instance = cls.__new__(cls)
        instance._pool = _validate_pool(pool)
        instance._owns_pool = False
        instance._closed = False
        instance._options = postgres_options or PostgresConnectionOptions(
            connection_timeout_seconds=connection_timeout_seconds,
            statement_timeout_ms=statement_timeout_ms,
            idle_transaction_timeout_ms=idle_transaction_timeout_ms,
            application_name=application_name,
            on_pool_error=on_pool_error,
        )
        instance._tenant_id = _normalize_tenant_id(tenant_id) if tenant_id is not None else None
        instance._access_role = _normalize_access_role(access_role, instance._tenant_id)
        instance._usage_backend = _normalize_backend(usage_backend, ("postgres", "clickhouse"), "usage_backend")
        instance._billing_payload_backend = _normalize_backend(
            billing_payload_backend,
            ("postgres", "s3"),
            "billing_payload_backend",
        )
        instance._provider_environment = _normalize_provider_environment(provider_environment)
        return instance

    def query(self, text: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """Execute parameterized SQL in a bounded, tenant-scoped transaction."""

        return self._run(
            lambda cursor: self._fetch_query(cursor, text, params),
            operation_name="query",
        )

    def callproc(self, name: str, params: Sequence[Any] | None = None) -> list[Any]:
        """Execute a PostgreSQL function and preserve scalar/table result shape."""

        def execute(cursor: Any) -> list[Any]:
            encoded = [
                psycopg2.extras.Json(value) if isinstance(value, (dict, list)) else value for value in (params or [])
            ]
            cursor.callproc(name, encoded)
            rows = cursor.fetchall() if cursor.description else []
            return [next(iter(row.values())) if len(row) == 1 else dict(row) for row in (rows or [])]

        return self._run(execute, operation_name=f"database RPC {name!r}")

    def close(self) -> None:
        """Close this client exactly once; borrowed pools remain caller-owned."""

        if self._closed:
            return
        self._closed = True
        pool = self._pool
        self._pool = None
        if self._owns_pool and pool is not None:
            try:
                pool.closeall()
            except BaseException as error:
                raise normalize_postgres_error(error, operation="close", phase="close") from error

    @property
    def connection_kwargs(self) -> dict[str, Any]:
        """Connection kwargs for one-off psycopg2 connections."""

        return self._connection_kwargs()

    def _run(self, callback: Callable[[Any], Any], *, operation_name: str) -> Any:
        if self._closed or self._pool is None:
            raise StoreClosedError("PostgreSQL client has been closed")
        pool = self._pool
        try:
            conn = pool.getconn()
        except BaseException as error:
            normalized = normalize_postgres_error(error, operation=operation_name, phase="connect")
            self._notify_pool_error(normalized)
            raise normalized from error

        phase = "begin"
        discard = False
        result: Any
        try:
            if getattr(conn, "autocommit", False):
                conn.autocommit = False
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                phase = "configure"
                self._configure(cursor)
                phase = "query"
                result = callback(cursor)
                phase = "commit"
                conn.commit()
            return result
        except BaseException as error:
            rollback_failed = False
            try:
                conn.rollback()
            except BaseException:
                rollback_failed = True
                discard = True
            normalized = normalize_postgres_error(
                error,
                operation=operation_name,
                phase=phase,
                indeterminate=phase in {"query", "commit"},
                rollback_failed=rollback_failed,
            )
            raise normalized from error
        finally:
            try:
                pool.putconn(conn, close=discard)
            except BaseException as release_error:
                self._notify_pool_error(
                    normalize_postgres_error(release_error, operation="release connection", phase="pool")
                )

    def _configure(self, cursor: Any) -> None:
        if self._access_role is not None:
            cursor.execute(f"SET LOCAL ROLE {self._access_role}")
        settings = [
            ("statement_timeout", str(self._options.statement_timeout_ms)),
            ("idle_in_transaction_session_timeout", str(self._options.idle_transaction_timeout_ms)),
        ]
        params: list[str] = []
        expressions: list[str] = []
        if self._tenant_id is not None:
            expressions.extend(
                [
                    "set_config('bursar.tenant_id', %s, true)",
                    "set_config('bursar.usage_backend', %s, true)",
                    "set_config('bursar.billing_payload_backend', %s, true)",
                    "set_config('bursar.provider_environment', %s, true)",
                ]
            )
            params.extend(
                [self._tenant_id, self._usage_backend, self._billing_payload_backend, self._provider_environment]
            )
        for name, value in settings:
            expressions.append(f"set_config('{name}', %s, true)")
            params.append(value)
        cursor.execute(f"SELECT {', '.join(expressions)}", params)
        cursor.execute("SET LOCAL search_path TO bursar, public")

    @staticmethod
    def _fetch_query(cursor: Any, text: str, params: Sequence[Any] | None) -> list[dict[str, Any]]:
        cursor.execute(text, list(params or []))
        if not cursor.description:
            return []
        return [dict(row) for row in cursor.fetchall()]

    def _connection_kwargs(self) -> dict[str, Any]:
        timeout = self._options.connection_timeout_seconds
        return {
            "connect_timeout": 0 if timeout == 0 else max(1, ceil(timeout)),
            "application_name": self._options.application_name.strip(),
        }

    def _notify_pool_error(self, error: BursarError) -> None:
        callback = self._options.on_pool_error
        if callback is None:
            return
        try:
            callback(error)
        except BaseException:
            # Observability hooks must never destabilize the SDK.
            return


def _normalize_tenant_id(tenant_id: str | UUID) -> str:
    try:
        return str(UUID(str(tenant_id)))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("tenant_id must be a UUID") from error


def _normalize_access_role(
    access_role: PostgresAccessRole | None,
    tenant_id: str | None,
) -> PostgresAccessRole | None:
    if access_role is None:
        return "bursar_client" if tenant_id is not None else None
    if access_role not in {"bursar_client", "bursar_operator"}:
        raise ValueError("access_role must be 'bursar_client' or 'bursar_operator'")
    return access_role


def _normalize_backend(value: str, choices: tuple[str, ...], name: str) -> str:
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}")
    return value


def _normalize_provider_environment(value: str) -> ProviderEnvironment:
    if value not in {"live", "test", "sandbox"}:
        raise ValueError("provider_environment must be 'live', 'test', or 'sandbox'")
    return cast(ProviderEnvironment, value)


def create_pool(
    dsn: str,
    min_connections: int = 1,
    max_connections: int | None = None,
    *,
    postgres_options: PostgresConnectionOptions | None = None,
) -> PostgresPool:
    """Create a configured psycopg2 ``ThreadedConnectionPool``."""

    options = postgres_options or PostgresConnectionOptions()
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("postgres connection string must not be empty")
    if not isinstance(min_connections, int) or isinstance(min_connections, bool) or min_connections < 1:
        raise ValueError("min_connections must be a positive integer")
    effective_max_connections = options.max_connections if max_connections is None else max_connections
    if (
        not isinstance(effective_max_connections, int)
        or isinstance(effective_max_connections, bool)
        or effective_max_connections < min_connections
    ):
        raise ValueError("max_connections must be at least min_connections")
    return _LazyPostgresPool(
        lambda: cast(
            PostgresPool,
            psycopg2.pool.ThreadedConnectionPool(
                min_connections,
                effective_max_connections,
                dsn,
                connect_timeout=0
                if options.connection_timeout_seconds == 0
                else max(1, ceil(options.connection_timeout_seconds)),
                application_name=options.application_name.strip(),
            ),
        )
    )


def close_pool(pool: PostgresPool | None) -> None:
    """Close all connections in a pool."""

    if pool is not None:
        pool.closeall()


__all__ = ["PostgresClient", "PostgresConnectionOptions", "PostgresPool", "close_pool", "create_pool"]
