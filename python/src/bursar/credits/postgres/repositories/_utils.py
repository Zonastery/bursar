from __future__ import annotations

from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from bursar.errors import StoreError

T = TypeVar("T", bound=BaseModel)


def validate_row[T: BaseModel](
    model: type[T],
    data: object,
    context: str,
    *,
    indeterminate: bool = False,
) -> T:
    """Validate a database row and keep adapter failures inside the SDK error contract."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise StoreError(
            f"{context}: row validation failed",
            cause=error,
            indeterminate=indeterminate,
            details={"context": context, "model": model.__name__},
        ) from error


def validate_first_row[T: BaseModel](rows: list[Any], model: type[T]) -> T | None:
    """Validate the optional singleton row returned by a lookup query."""
    if not rows:
        return None
    row = optional_mapping_row(rows, f"{model.__name__} lookup")
    return validate_row(model, row, f"{model.__name__} lookup")


def require_row(rows: list[Any] | None, context: str) -> Any:
    """Require the single-row envelope promised by a scalar or mutation RPC."""
    row_count = len(rows) if rows is not None else 0
    if row_count != 1 or rows is None or rows[0] is None:
        raise StoreError(
            f"{context}: expected exactly one result row, received {row_count}",
            indeterminate=True,
            details={"context": context, "row_count": row_count},
        )
    return rows[0]


def require_mapping_row(rows: list[Any] | None, context: str) -> dict[str, Any]:
    """Require the single object row promised by a mutation RPC."""
    row = require_row(rows, context)
    if not isinstance(row, dict):
        raise StoreError(
            f"{context}: expected an object result",
            indeterminate=True,
            details={"context": context},
        )
    return row


def optional_mapping_row(rows: list[Any] | None, context: str) -> dict[str, Any] | None:
    """Return an optional singleton query row and reject ambiguous results."""
    if not rows:
        return None
    if len(rows) != 1:
        raise StoreError(
            f"{context}: expected at most one result row, received {len(rows)}",
            details={"context": context, "row_count": len(rows)},
        )
    row = rows[0]
    if not isinstance(row, dict):
        raise StoreError(
            f"{context}: expected an object result",
            details={"context": context},
        )
    return row


def require_boolean_result(rows: list[Any] | None, key: str, context: str) -> bool:
    """Require an actual PostgreSQL boolean from a mutation result row."""
    value = require_mapping_row(rows, context).get(key)
    if type(value) is not bool:
        raise StoreError(
            f"{context}: expected a boolean {key!r} result",
            indeterminate=True,
            details={"context": context, "field": key},
        )
    return value


def require_identifier_result(rows: list[Any] | None, key: str, context: str) -> str:
    """Require a UUID identifier from a mutation row."""
    value = require_mapping_row(rows, context).get(key)
    try:
        identifier = str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise StoreError(
            f"{context}: expected a UUID {key!r} result",
            indeterminate=True,
            details={"context": context, "field": key},
        ) from error
    return identifier


def require_bigint_identifier_result(rows: list[Any] | None, key: str, context: str) -> str:
    """Require a positive signed-64-bit identity value from a mutation row."""
    value = require_mapping_row(rows, context).get(key)
    if type(value) is int:
        identifier = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        identifier = int(value)
    else:
        identifier = 0
    if not 1 <= identifier <= 9_223_372_036_854_775_807:
        raise StoreError(
            f"{context}: expected a positive bigint {key!r} result",
            indeterminate=True,
            details={"context": context, "field": key},
        )
    return str(identifier)


def validate_non_empty(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def validate_non_negative(value: int | float | Decimal, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def validate_amount(value: str | Decimal, name: str) -> None:
    """Validate that a string or Decimal amount is non-negative."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{name} must be a non-empty amount, got {value!r}")
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if dec < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def unwrap_jsonb(rows: list[Any]) -> dict[str, Any] | None:
    """Unwrap a single-row JSONB result from an RPC call.

    Handles five result shapes with distinct exit paths:
        1. Empty or multi-row result → None
        2. Single-row, single-key dict where the value is None → None
        3. Single-row, single-key dict where the value is a dict → unwrap (return inner dict)
        4. Single-row dict with multiple keys → return row as-is
        5. Non-dict row → None

    Expected RPC result shapes:
        - `SELECT * FROM some_rpc(...)` → list of dicts (column_name → value)
        - `SELECT jsonb_build_object(...)` → list with one dict and one key
        - RPCs returning `SETOF record` or tables → multiple rows, each as a dict
    """
    if not rows or len(rows) != 1:
        return None
    row = rows[0]
    if isinstance(row, dict):
        keys = list(row.keys())
        if len(keys) == 1:
            v = row[keys[0]]
            if v is None:
                return None
            if isinstance(v, dict):
                return v
        return row
    return None
