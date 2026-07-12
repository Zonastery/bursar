from __future__ import annotations

from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def validate_first_row(rows: list[Any], model: type[T]) -> T | None:
    """Return the first row as a validated model if present, else None."""
    if not rows or not isinstance(rows[0], dict):
        return None
    return model.model_validate(rows[0])


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
