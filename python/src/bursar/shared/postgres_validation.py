"""Postgres validation utilities — mirrors JS SDK's ``shared/postgres-validation.ts``."""

from __future__ import annotations

from typing import Any

from bursar.errors import StoreError


def unwrap_jsonb(rows: list[Any]) -> dict[str, Any] | None:
    """Unwrap the single-row JSONB result shape used by PostgreSQL RPCs."""
    if len(rows) != 1:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    if len(row) == 1:
        value = next(iter(row.values()))
        if value is None:
            return None
        if isinstance(value, dict):
            return value
    return row


def safe_parse(model_class: type, data: dict, context: str) -> Any:
    """Validate data against a Pydantic model, matching JS safeParse.
    Raises ValueError with descriptive message on failure."""
    try:
        return model_class.model_validate(data)
    except Exception as exc:
        raise StoreError(f"{context}: schema validation failed — {exc}") from exc


def pg_bool(value: Any) -> bool:
    """Parse PostgreSQL boolean from various input types. Matches JS pgBoolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value in ("true", "t", "1")
    if isinstance(value, (int, float)):
        return value != 0
    return False


def validate_safe_identifier(name: str) -> str:
    """Validate a SQL identifier to prevent injection.

    Args:
        name: The identifier to validate.

    Returns:
        The validated identifier (safe for use in queries).

    Raises:
        ValueError: If the identifier contains unsafe characters.
    """
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name
