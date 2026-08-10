"""OpenTelemetry API-only adapter for Bursar instrumentation."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import TypeVar

from opentelemetry import metrics, trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from bursar.telemetry.core import (
    BURSAR_INSTRUMENTATION_SCOPE,
    BURSAR_INSTRUMENTATION_VERSION,
    TelemetryAttributes,
    TelemetryAttributeValue,
    set_default_instrumentation,
    telemetry_error_attributes,
    telemetry_operation_attributes,
)

T = TypeVar("T")


class OpenTelemetryInstrumentation:
    """Emit spans and metrics through host-configured OpenTelemetry providers."""

    def __init__(self, *, tracer: Tracer | None = None, meter: Meter | None = None) -> None:
        self._tracer = tracer or trace.get_tracer(
            BURSAR_INSTRUMENTATION_SCOPE,
            BURSAR_INSTRUMENTATION_VERSION,
        )
        selected_meter = meter or metrics.get_meter(
            BURSAR_INSTRUMENTATION_SCOPE,
            BURSAR_INSTRUMENTATION_VERSION,
        )
        self._operation_counter = selected_meter.create_counter(
            "bursar.operation.count",
            unit="{operation}",
            description="Completed Bursar operations",
        )
        self._operation_duration = selected_meter.create_histogram(
            "bursar.operation.duration",
            unit="s",
            description="Bursar operation duration",
        )

    def run(
        self,
        operation: str,
        attributes: TelemetryAttributes | None,
        callback: Callable[[], T],
    ) -> T:
        base_attributes = telemetry_operation_attributes(operation, attributes)
        span_name = f"bursar.{base_attributes['bursar.operation']}"
        with self._tracer.start_as_current_span(
            span_name,
            attributes=base_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            return self._run_in_span(span, base_attributes, callback)

    def _run_in_span(
        self,
        span: Span,
        base_attributes: dict[str, TelemetryAttributeValue],
        callback: Callable[[], T],
    ) -> T:
        started_at = perf_counter()
        try:
            result = callback()
            completed = {**base_attributes, "bursar.outcome": "success"}
            span.set_attributes(completed)
            span.set_status(Status(StatusCode.OK))
            self._record_metrics(started_at, completed)
            return result
        except BaseException as error:
            completed = {
                **base_attributes,
                "bursar.outcome": "error",
                **telemetry_error_attributes(error),
            }
            span.set_attributes(completed)
            # Do not add a status description or exception event: both may
            # include a raw message, SQL text, identifier, or provider payload.
            span.set_status(Status(StatusCode.ERROR))
            self._record_metrics(started_at, completed)
            raise
        finally:
            # start_as_current_span owns span.end() and active-context cleanup.
            pass

    def _record_metrics(
        self,
        started_at: float,
        attributes: dict[str, TelemetryAttributeValue],
    ) -> None:
        self._operation_counter.add(1, attributes)
        self._operation_duration.record(max(0.0, perf_counter() - started_at), attributes)


def create_opentelemetry_instrumentation(
    *,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
) -> OpenTelemetryInstrumentation:
    """Create an API-only adapter using host-provided or global API providers."""

    return OpenTelemetryInstrumentation(tracer=tracer, meter=meter)


def enable_opentelemetry(
    *,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
) -> Callable[[], None]:
    """Enable the adapter for subsequently constructed Bursar services."""

    return set_default_instrumentation(create_opentelemetry_instrumentation(tracer=tracer, meter=meter))


__all__ = [
    "OpenTelemetryInstrumentation",
    "create_opentelemetry_instrumentation",
    "enable_opentelemetry",
]
