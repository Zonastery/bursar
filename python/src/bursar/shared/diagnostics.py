"""Normalization for diagnostic text persisted by SQL workflows."""

from __future__ import annotations

PERSISTED_DIAGNOSTIC_MAX_CHARACTERS = 8_192


def bounded_diagnostic_message(
    value: object | None,
    fallback: str = "operation_failed",
) -> str:
    """Return trimmed, non-empty text accepted by diagnostic SQL constraints."""
    message = "" if value is None else str(value)
    message = message.replace("\x00", "\ufffd").strip()
    if not message and isinstance(value, BaseException):
        message = type(value).__name__

    normalized_fallback = fallback.replace("\x00", "\ufffd").strip() or "operation_failed"
    return (message or normalized_fallback)[:PERSISTED_DIAGNOSTIC_MAX_CHARACTERS]


def optional_bounded_diagnostic_message(value: object | None) -> str | None:
    """Normalize an optional diagnostic while preserving an absent value."""
    return None if value is None else bounded_diagnostic_message(value)
