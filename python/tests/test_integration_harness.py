"""Regression coverage for the real-PostgreSQL integration gate."""

import pytest

from tests.conftest import _handle_unavailable_postgres, _require_external_database_reset_opt_in


def test_required_postgres_failure_cannot_become_a_green_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BURSAR_REQUIRE_POSTGRES_TESTS", "1")

    with pytest.raises(pytest.fail.Exception, match="database unavailable"):
        _handle_unavailable_postgres("database unavailable")


def test_optional_local_postgres_failure_is_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BURSAR_REQUIRE_POSTGRES_TESTS", raising=False)

    with (
        pytest.warns(UserWarning, match="database unavailable"),
        pytest.raises(
            pytest.skip.Exception,
            match="database unavailable",
        ),
    ):
        _handle_unavailable_postgres("database unavailable")


def test_external_database_reset_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BURSAR_ALLOW_DATABASE_RESET", raising=False)
    with pytest.raises(pytest.fail.Exception, match="Refusing to reset externally supplied"):
        _require_external_database_reset_opt_in()

    monkeypatch.setenv("BURSAR_ALLOW_DATABASE_RESET", "1")
    _require_external_database_reset_opt_in()
