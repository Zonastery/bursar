"""Vendor-neutral, no-op-by-default instrumentation primitives."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
from threading import RLock
from typing import Protocol, TypeVar, runtime_checkable

from bursar.shared.diagnostics import diagnostic_error_code, diagnostic_error_type

try:
    BURSAR_INSTRUMENTATION_VERSION = version("bursar")
except PackageNotFoundError:  # pragma: no cover - source trees are installed in tests
    BURSAR_INSTRUMENTATION_VERSION = "0+unknown"

BURSAR_INSTRUMENTATION_SCOPE = "bursar"

TelemetryAttributeValue = str | int | float | bool
TelemetryAttributes = Mapping[str, TelemetryAttributeValue]

_ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "bursar.operation",
        "bursar.outcome",
        "bursar.backend",
        "bursar.provider",
        "error.type",
        "error.code",
    }
)
_MAX_ATTRIBUTE_LENGTH = 64
_CAMEL_CASE_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_UNSAFE_TOKEN_CHARS = re.compile(r"[^a-zA-Z0-9._-]+")
_TOKEN_EDGES = re.compile(r"^[_\-.]+|[_\-.]+$")

T = TypeVar("T")


def _normalize_token(value: object, fallback: str | None = None) -> str | None:
    if not isinstance(value, str):
        return fallback
    normalized = _CAMEL_CASE_BOUNDARY.sub(r"\1_\2", value.strip())
    normalized = _UNSAFE_TOKEN_CHARS.sub("_", normalized)
    normalized = _TOKEN_EDGES.sub("", normalized).lower()[:_MAX_ATTRIBUTE_LENGTH]
    return normalized or fallback


def sanitize_telemetry_attributes(
    attributes: Mapping[str, object] | None = None,
) -> dict[str, TelemetryAttributeValue]:
    """Discard unknown, unbounded, and non-scalar telemetry attributes."""

    sanitized: dict[str, TelemetryAttributeValue] = {}
    for key, value in (attributes or {}).items():
        if key not in _ALLOWED_ATTRIBUTE_KEYS:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)) and isfinite(value):
            sanitized[key] = value
        else:
            normalized = _normalize_token(value)
            if normalized is not None:
                sanitized[key] = normalized
    return sanitized


def telemetry_operation_attributes(
    operation: str,
    attributes: Mapping[str, object] | None = None,
) -> dict[str, TelemetryAttributeValue]:
    """Build bounded base attributes for one Bursar operation."""

    return {
        **sanitize_telemetry_attributes(attributes),
        "bursar.operation": _normalize_token(operation, "unknown") or "unknown",
    }


def telemetry_error_attributes(error: BaseException) -> dict[str, str]:
    """Normalize an exception without reading or recording its message."""

    error_type = _normalize_token(diagnostic_error_type(error), "unknown_error") or "unknown_error"
    error_code = _normalize_token(diagnostic_error_code(error))
    return {
        "error.type": error_type,
        **({} if error_code is None else {"error.code": error_code}),
    }


@runtime_checkable
class Instrumentation(Protocol):
    """Vendor-neutral execution boundary used by Bursar core services."""

    def run(
        self,
        operation: str,
        attributes: TelemetryAttributes | None,
        callback: Callable[[], T],
    ) -> T: ...


class NoopInstrumentation:
    """Execute normally without emitting traces or metrics."""

    def run(
        self,
        operation: str,
        attributes: TelemetryAttributes | None,
        callback: Callable[[], T],
    ) -> T:
        del operation, attributes
        return callback()


NOOP_INSTRUMENTATION: Instrumentation = NoopInstrumentation()

_default_instrumentation: Instrumentation = NOOP_INSTRUMENTATION
_default_lock = RLock()


class _InstrumentationRegistration:
    __slots__ = ("active", "instrumentation")

    def __init__(self, instrumentation: Instrumentation) -> None:
        self.instrumentation = instrumentation
        self.active = True


_instrumentation_registrations: list[_InstrumentationRegistration] = []


def _refresh_default_instrumentation() -> None:
    global _default_instrumentation
    while _instrumentation_registrations and not _instrumentation_registrations[-1].active:
        _instrumentation_registrations.pop()
    _default_instrumentation = (
        _instrumentation_registrations[-1].instrumentation if _instrumentation_registrations else NOOP_INSTRUMENTATION
    )


def get_default_instrumentation() -> Instrumentation:
    """Return the instrumentation selected by the embedding application."""

    with _default_lock:
        return _default_instrumentation


def set_default_instrumentation(instrumentation: Instrumentation | None) -> Callable[[], None]:
    """Select Bursar instrumentation and return an idempotent restore callback."""

    selected = instrumentation or NOOP_INSTRUMENTATION
    if not callable(getattr(selected, "run", None)):
        raise TypeError("instrumentation must provide run()")
    with _default_lock:
        registration = _InstrumentationRegistration(selected)
        _instrumentation_registrations.append(registration)
        _refresh_default_instrumentation()

    def restore() -> None:
        with _default_lock:
            if not registration.active:
                return
            registration.active = False
            _refresh_default_instrumentation()

    return restore


__all__ = [
    "BURSAR_INSTRUMENTATION_SCOPE",
    "BURSAR_INSTRUMENTATION_VERSION",
    "Instrumentation",
    "NOOP_INSTRUMENTATION",
    "NoopInstrumentation",
    "TelemetryAttributes",
    "TelemetryAttributeValue",
    "get_default_instrumentation",
    "sanitize_telemetry_attributes",
    "set_default_instrumentation",
    "telemetry_error_attributes",
    "telemetry_operation_attributes",
]
