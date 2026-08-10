"""Optional, vendor-neutral Bursar instrumentation contracts.

The OpenTelemetry adapter intentionally lives in
``bursar.telemetry.opentelemetry`` so importing :mod:`bursar` never imports an
optional observability package.
"""

from bursar.telemetry.core import (
    BURSAR_INSTRUMENTATION_SCOPE,
    BURSAR_INSTRUMENTATION_VERSION,
    NOOP_INSTRUMENTATION,
    Instrumentation,
    NoopInstrumentation,
    TelemetryAttributes,
    TelemetryAttributeValue,
    get_default_instrumentation,
    sanitize_telemetry_attributes,
    set_default_instrumentation,
    telemetry_error_attributes,
    telemetry_operation_attributes,
)

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
