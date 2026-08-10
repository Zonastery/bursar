"""Normalization for diagnostic text persisted by SQL workflows."""

from __future__ import annotations

from bursar.errors import BURSAR_ERROR_CODES, BursarError

PERSISTED_DIAGNOSTIC_MAX_CHARACTERS = 8_192
DIAGNOSTIC_CODE_MAX_CHARACTERS = 128


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


def diagnostic_error_code(value: object | None) -> str | None:
    """Return an SDK-owned error code, never an arbitrary exception property."""

    if not isinstance(value, BursarError):
        return None
    try:
        code = value.code
    except Exception:
        return None
    return code if code in BURSAR_ERROR_CODES else None


def diagnostic_error_type(value: object | None) -> str:
    """Return a fixed type without trusting arbitrary class or instance names."""

    if isinstance(value, BursarError):
        return "BursarError"
    for error_type in (
        RuntimeError,
        TypeError,
        ValueError,
        LookupError,
        ArithmeticError,
        AssertionError,
        OSError,
        Exception,
    ):
        if isinstance(value, error_type):
            return error_type.__name__
    return "UnknownError"


def persisted_diagnostic_summary(
    value: object | None,
    fallback: str = "operation_failed",
) -> str:
    """Return a persistence-safe code without exception messages or metadata."""

    operation = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in bounded_diagnostic_message(fallback, "operation_failed")
    )[:DIAGNOSTIC_CODE_MAX_CHARACTERS]
    diagnostic = diagnostic_error_code(value) or diagnostic_error_type(value)
    return f"{operation or 'operation_failed'}:{diagnostic}"
