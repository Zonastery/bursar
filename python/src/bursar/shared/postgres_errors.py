"""Stable PostgreSQL/transport error classification for the Python SDK."""

from __future__ import annotations

import re
from typing import Any

from bursar.errors import (
    BursarError,
    StoreError,
    StoreTimeoutError,
    StoreUnavailableError,
    is_bursar_error,
)

PostgresOperationPhase = str

_RETRYABLE_SQLSTATES = {
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "55P03",  # lock_not_available
    "57P01",  # admin_shutdown
    "57P02",  # crash_shutdown
    "57P03",  # cannot_connect_now
    "53300",  # too_many_connections
}
_RETRYABLE_NETWORK_CODES = {
    "EAI_AGAIN",
    "ECONNABORTED",
    "ECONNREFUSED",
    "ECONNRESET",
    "EHOSTDOWN",
    "EHOSTUNREACH",
    "ENETDOWN",
    "ENETRESET",
    "ENETUNREACH",
    "ENOTFOUND",
    "EPIPE",
    "ESOCKETTIMEDOUT",
    "ETIMEDOUT",
}


def _code(error: BaseException) -> str | None:
    for attribute in ("pgcode", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value:
            return value.upper()
    return None


def _message(error: BaseException) -> str:
    return str(error) or type(error).__name__


def _is_sqlstate(code: str | None) -> bool:
    return code is not None and code not in _RETRYABLE_NETWORK_CODES and bool(re.fullmatch(r"[0-9A-Z]{5}", code))


def _is_timeout(error: BaseException, code: str | None) -> bool:
    if isinstance(error, TimeoutError) or code in {"57014", "ETIMEDOUT", "ESOCKETTIMEDOUT"}:
        return True
    message = _message(error)
    return message in {
        "Query read timeout",
        "timeout expired",
        "timeout exceeded when trying to connect",
        "Connection terminated due to connection timeout",
    } or bool(re.search(r"\b(?:query|connection|statement) timed? ?out\b", message, re.IGNORECASE))


def _is_unavailable(error: BaseException, code: str | None) -> bool:
    return bool(
        isinstance(error, ConnectionError)
        or (code and code.startswith("08"))
        or (code and (code in _RETRYABLE_SQLSTATES or code in _RETRYABLE_NETWORK_CODES))
        or re.match(
            r"^(?:Connection terminated (?:unexpectedly|due to connection timeout)|"
            r"(?:server )?closed the connection unexpectedly|"
            r"connection (?:is )?already closed|"
            r"could not connect to server|"
            r"connection pool (?:is )?exhausted|"
            r"(?:temporary failure in name resolution|could not translate host name|"
            r"no route to host))",
            _message(error),
            re.I,
        )
    )


def _has_indeterminate_outcome(error: BaseException, code: str | None) -> bool:
    return bool(
        isinstance(error, (ConnectionError, TimeoutError))
        or (code and (code in _RETRYABLE_NETWORK_CODES or code.startswith("08")))
        or (code is None and _message(error) == "Query read timeout")
        or re.match(
            r"^Connection terminated (?:unexpectedly|due to connection timeout)",
            _message(error),
            re.I,
        )
    )


def normalize_postgres_error(
    error: BaseException,
    *,
    operation: str = "operation",
    phase: PostgresOperationPhase | None = None,
    indeterminate: bool = False,
    rollback_failed: bool = False,
) -> BursarError:
    """Map psycopg2 and socket failures to stable SDK errors.

    PostgreSQL SQLSTATE and OS/network codes are preferred over localized
    messages. The message fallbacks are limited to stable psycopg2 timeout
    strings that do not carry a code.
    """

    if is_bursar_error(error):
        return error
    code = _code(error)
    details: dict[str, Any] = {
        "datastore": "postgresql",
        "operation": operation,
    }
    if phase is not None:
        details["phase"] = phase
    if _is_sqlstate(code):
        details["sql_state"] = code
    elif code:
        details["network_code"] = code
    if rollback_failed:
        details["rollback_failed"] = True
    options = {
        "cause": error,
        "details": details,
        "indeterminate": indeterminate and _has_indeterminate_outcome(error, code),
    }
    if _is_timeout(error, code):
        return StoreTimeoutError(f"PostgreSQL {operation} timed out", **options)
    if _is_unavailable(error, code):
        return StoreUnavailableError(f"PostgreSQL {operation} is temporarily unavailable", **options)
    return StoreError(f"PostgreSQL {operation} failed", **options)


__all__ = ["PostgresOperationPhase", "normalize_postgres_error"]
