from __future__ import annotations

from decimal import Decimal
from typing import Any


def validate_non_empty(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def validate_non_negative(value: int | float | Decimal, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def validate_amount(value: str | Decimal, name: str) -> None:
    if isinstance(value, Decimal) and value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def unwrap_jsonb(rows: list[Any]) -> dict[str, Any] | None:
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
