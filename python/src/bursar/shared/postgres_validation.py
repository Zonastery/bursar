"""Postgres validation utilities — mirrors JS SDK's ``shared/postgres-validation.ts``."""

from __future__ import annotations


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
