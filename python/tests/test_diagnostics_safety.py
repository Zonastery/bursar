from bursar.errors import BursarError
from bursar.shared.diagnostics import persisted_diagnostic_summary


def test_persisted_diagnostic_keeps_bursar_code_without_message() -> None:
    error = BursarError("token=secret and account=123")

    assert persisted_diagnostic_summary(error, "outbox_delivery_failed") == ("outbox_delivery_failed:BURSAR_ERROR")


def test_persisted_diagnostic_keeps_only_native_error_type() -> None:
    error = RuntimeError("https://user:password@example.test/path?token=secret")

    assert persisted_diagnostic_summary(error, "billing event failed") == ("billing_event_failed:RuntimeError")


def test_persisted_diagnostic_rejects_arbitrary_string() -> None:
    assert persisted_diagnostic_summary("customer payload: secret") == ("operation_failed:UnknownError")


def test_persisted_diagnostic_rejects_attacker_controlled_codes() -> None:
    class CustomerSecretError(Exception):
        @property
        def code(self) -> str:
            raise RuntimeError("code getter secret")

    class MutatedBursarError(BursarError):
        code = "customer_12345"

    assert persisted_diagnostic_summary(CustomerSecretError("private")) == "operation_failed:Exception"
    assert persisted_diagnostic_summary(MutatedBursarError("private")) == "operation_failed:BursarError"
