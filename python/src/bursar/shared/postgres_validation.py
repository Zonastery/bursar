"""Postgres validation utilities — mirrors JS SDK's ``shared/postgres-validation.ts``."""

from __future__ import annotations

from typing import Any


def unwrap_jsonb(rows: list[dict]) -> dict | None:
    """Extract first row from query result. Matches JS unwrapJsonb."""
    if not rows:
        return None
    return rows[0]


def safe_parse(model_class: type, data: dict, context: str) -> Any:
    """Validate data against a Pydantic model, matching JS safeParse.
    Raises ValueError with descriptive message on failure."""
    try:
        return model_class.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Invalid data in {context}: {exc}") from exc


def pg_bool(value: Any) -> bool:
    """Parse PostgreSQL boolean from various input types. Matches JS pgBoolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "t", "yes", "y", "1")
    if isinstance(value, int):
        return value == 1
    return bool(value)


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
