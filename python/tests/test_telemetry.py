from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from opentelemetry import metrics, trace
from opentelemetry.trace import StatusCode

from bursar.credits.service import CreditsService
from bursar.credits.service_types import CreditsServiceOptions, GrantSubscriptionCycleOptions, ReserveOptions
from bursar.errors import StoreTimeoutError
from bursar.shared.postgres_client import PostgresClient
from bursar.telemetry import (
    BURSAR_INSTRUMENTATION_SCOPE,
    BURSAR_INSTRUMENTATION_VERSION,
    NOOP_INSTRUMENTATION,
    TelemetryAttributes,
    get_default_instrumentation,
    sanitize_telemetry_attributes,
    set_default_instrumentation,
    telemetry_error_attributes,
)
from bursar.telemetry.opentelemetry import (
    OpenTelemetryInstrumentation,
    create_opentelemetry_instrumentation,
)

T = TypeVar("T")

EXPECTED_OPERATIONS = json.loads(
    (Path(__file__).parents[2] / "tests" / "parity" / "telemetry_operations.json").read_text()
)


def test_telemetry_never_trusts_arbitrary_error_names_or_codes() -> None:
    class CustomerSecretError(Exception):
        code = "tenant_67890"

    assert telemetry_error_attributes(CustomerSecretError("private")) == {"error.type": "exception"}


def test_nested_defaults_restore_correctly_out_of_order() -> None:
    first = RecordingInstrumentation()
    second = RecordingInstrumentation()
    restore_first = set_default_instrumentation(first)
    restore_second = set_default_instrumentation(second)

    restore_first()
    assert get_default_instrumentation() is second
    restore_second()
    assert get_default_instrumentation() is NOOP_INSTRUMENTATION


class RecordingInstrumentation:
    def __init__(self) -> None:
        self.operations: list[tuple[str, TelemetryAttributes | None]] = []

    def run(
        self,
        operation: str,
        attributes: TelemetryAttributes | None,
        callback: Callable[[], T],
    ) -> T:
        self.operations.append((operation, attributes))
        return callback()


class OpenTelemetryDoubles:
    def __init__(self) -> None:
        self.active = False
        self.span_name = ""
        self.span_attributes: dict[str, object] = {}
        self.status_codes: list[StatusCode] = []
        self.counter: list[tuple[int, Mapping[str, object] | None]] = []
        self.histogram: list[tuple[float, Mapping[str, object] | None]] = []

        owner = self

        class FakeSpan:
            def set_attributes(self, attributes: Mapping[str, object]) -> None:
                owner.span_attributes.update(attributes)

            def set_status(self, status: Any) -> None:
                owner.status_codes.append(status.status_code)

        class FakeTracer:
            @contextmanager
            def start_as_current_span(
                self,
                name: str,
                *,
                attributes: Mapping[str, object],
                record_exception: bool,
                set_status_on_exception: bool,
            ) -> Iterator[FakeSpan]:
                assert record_exception is False
                assert set_status_on_exception is False
                owner.span_name = name
                owner.span_attributes.update(attributes)
                owner.active = True
                try:
                    yield FakeSpan()
                finally:
                    owner.active = False

        class FakeCounter:
            def add(self, value: int, attributes: Mapping[str, object] | None = None) -> None:
                owner.counter.append((value, attributes))

        class FakeHistogram:
            def record(self, value: float, attributes: Mapping[str, object] | None = None) -> None:
                owner.histogram.append((value, attributes))

        class FakeMeter:
            def create_counter(self, *_args: Any, **_kwargs: Any) -> FakeCounter:
                return FakeCounter()

            def create_histogram(self, *_args: Any, **_kwargs: Any) -> FakeHistogram:
                return FakeHistogram()

        self.tracer = cast(Any, FakeTracer())
        self.meter = cast(Any, FakeMeter())


def test_noop_instrumentation_preserves_success_and_original_failure() -> None:
    assert NOOP_INSTRUMENTATION.run("credits.reserve", None, lambda: "ok") == "ok"

    failure = RuntimeError("private failure text")

    def fail() -> None:
        raise failure

    with pytest.raises(RuntimeError) as captured:
        NOOP_INSTRUMENTATION.run("credits.reserve", None, fail)
    assert captured.value is failure


def test_attribute_allowlist_keeps_only_bounded_low_cardinality_values() -> None:
    assert sanitize_telemetry_attributes(
        {
            "bursar.backend": "Postgres / Primary",
            "bursar.provider": "Dodo Payments",
            "bursar.outcome": "SUCCESS",
            "tenant.id": "tenant-secret",
            "user_id": "user-secret",
            "idempotency_key": "request-secret",
            "metadata": {"prompt": "private prompt"},
            "error.message": "private error",
        }
    ) == {
        "bursar.backend": "postgres_primary",
        "bursar.provider": "dodo_payments",
        "bursar.outcome": "success",
    }


def test_opentelemetry_adapter_records_success_inside_the_active_context() -> None:
    doubles = OpenTelemetryDoubles()
    instrumentation = OpenTelemetryInstrumentation(tracer=doubles.tracer, meter=doubles.meter)

    def reserve() -> str:
        assert doubles.active is True
        return "reserved"

    assert (
        instrumentation.run(
            "credits.reserve",
            cast(TelemetryAttributes, {"bursar.backend": "postgres", "tenant.id": "tenant-secret"}),
            reserve,
        )
        == "reserved"
    )
    assert doubles.span_name == "bursar.credits.reserve"
    assert doubles.span_attributes == {
        "bursar.operation": "credits.reserve",
        "bursar.backend": "postgres",
        "bursar.outcome": "success",
    }
    assert doubles.status_codes == [StatusCode.OK]
    assert len(doubles.counter) == 1
    assert len(doubles.histogram) == 1
    assert "tenant-secret" not in repr(doubles.__dict__)


def test_opentelemetry_adapter_records_normalized_error_without_message() -> None:
    doubles = OpenTelemetryDoubles()
    instrumentation = OpenTelemetryInstrumentation(tracer=doubles.tracer, meter=doubles.meter)
    failure = StoreTimeoutError("database URL and tenant are private")

    def fail() -> None:
        raise failure

    with pytest.raises(StoreTimeoutError) as captured:
        instrumentation.run("postgres.rpc", {"bursar.backend": "postgres"}, fail)
    assert captured.value is failure
    assert doubles.span_attributes == {
        "bursar.operation": "postgres.rpc",
        "bursar.backend": "postgres",
        "bursar.outcome": "error",
        "error.type": "bursar_error",
        "error.code": "store_timeout",
    }
    assert doubles.status_codes == [StatusCode.ERROR]
    assert "database URL and tenant are private" not in repr(doubles.__dict__)


def test_opentelemetry_api_without_sdk_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, tuple[str, str]] = {}
    no_op_tracer = trace.NoOpTracerProvider().get_tracer("test")
    no_op_meter = metrics.NoOpMeterProvider().get_meter("test")

    def get_tracer(name: str, version: str):
        calls["tracer"] = (name, version)
        return no_op_tracer

    def get_meter(name: str, version: str):
        calls["meter"] = (name, version)
        return no_op_meter

    monkeypatch.setattr(trace, "get_tracer", get_tracer)
    monkeypatch.setattr(metrics, "get_meter", get_meter)

    instrumentation = create_opentelemetry_instrumentation()
    assert instrumentation.run("credits.release", None, lambda: "released") == "released"
    assert calls == {
        "tracer": (BURSAR_INSTRUMENTATION_SCOPE, BURSAR_INSTRUMENTATION_VERSION),
        "meter": (BURSAR_INSTRUMENTATION_SCOPE, BURSAR_INSTRUMENTATION_VERSION),
    }


class FakeCursor:
    description: list[object] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, text: str, params: list[object] | None = None) -> None:
        del params
        if text in {"SELECT 1", "SELECT private"}:
            self.description = [object()]
        else:
            self.description = None

    def callproc(self, _name: str, _params: list[object]) -> None:
        self.description = [object()]

    def fetchall(self) -> list[dict[str, object]]:
        return [{"value": 1}]


class FakeConnection:
    autocommit = False

    def cursor(self, **_kwargs: object) -> FakeCursor:
        return FakeCursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def getconn(self) -> FakeConnection:
        return self.connection

    def putconn(self, _conn: object, _key: object = None, close: bool = False) -> None:
        return None

    def closeall(self) -> None:
        return None


def test_postgres_boundaries_do_not_attach_sql_or_parameters() -> None:
    instrumentation = RecordingInstrumentation()
    client = PostgresClient.from_pool(cast(Any, FakePool()), instrumentation=instrumentation)

    assert client.query("SELECT private", ["private-id"]) == [{"value": 1}]
    assert client.callproc("private_rpc", ["private-id"]) == [1]

    assert instrumentation.operations == [
        ("postgres.query", {"bursar.backend": "postgres"}),
        ("postgres.rpc", {"bursar.backend": "postgres"}),
    ]
    assert sorted(operation for operation in EXPECTED_OPERATIONS if operation.startswith("postgres.")) == [
        "postgres.query",
        "postgres.rpc",
    ]
    assert "private-id" not in repr(instrumentation.operations)
    assert "private_rpc" not in repr(instrumentation.operations)


class ThrowingStore:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def __getattr__(self, _name: str) -> Callable[..., Any]:
        def fail(*_args: object, **_kwargs: object) -> None:
            raise self.failure

        return fail


def test_credit_operations_match_the_shared_javascript_python_contract() -> None:
    instrumentation = RecordingInstrumentation()
    failure = RuntimeError("stop after entering operation")
    credits = CreditsService(
        store=cast(Any, ThrowingStore(failure)),
        options=CreditsServiceOptions(instrumentation=instrumentation),
    )

    operations: list[Callable[[], object]] = [
        lambda: credits.deduct(
            "private-user",
            cast(Any, object()),
            idempotency_key="private-key",
        ),
        lambda: credits.add_credits("private-user", 1, idempotency_key="private-key"),
        lambda: credits.execute_grant_program(cast(Any, {})),
        lambda: credits.grant_subscription_cycle(
            "private-user",
            1,
            GrantSubscriptionCycleOptions(idempotency_key="private-key"),
        ),
        lambda: credits.refund_credits("private-entry", idempotency_key="private-key"),
        lambda: credits.release("private-user", "private-lease"),
        lambda: credits.reserve(
            "private-user",
            Decimal(1),
            ReserveOptions(idempotency_key="private-key"),
        ),
        lambda: credits.settle("private-user", "private-lease", Decimal(1)),
    ]
    for operation in operations:
        with pytest.raises(RuntimeError, match="stop after entering operation"):
            operation()

    observed = sorted(operation for operation, _attributes in instrumentation.operations)
    assert observed == sorted(operation for operation in EXPECTED_OPERATIONS if operation.startswith("credits."))
    assert sorted([*observed, "postgres.query", "postgres.rpc"]) == sorted(EXPECTED_OPERATIONS)
    assert "private-user" not in repr(instrumentation.operations)
    assert "private-key" not in repr(instrumentation.operations)
