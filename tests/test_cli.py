"""Tests for ducto CLI argument parsing and error handling."""

from __future__ import annotations

import pytest


def _run(*args: str) -> None:
    """Invoke ducto CLI main() with given args, capturing sys.exit."""
    import sys

    from ducto.__main__ import main

    old_argv = sys.argv
    try:
        sys.argv = ["ducto", *args]
        main()
    finally:
        sys.argv = old_argv


class TestMigrate:
    def test_migrate_no_args_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run("migrate")


class TestPricing:
    def test_pricing_no_subcommand_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run("pricing")

    def test_pricing_unknown_subcommand_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run("pricing", "fly")

    def test_pricing_set_no_file_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run("pricing", "set")

    def test_pricing_set_file_not_found_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run("pricing", "set", "/nonexistent/file.json")

    def test_pricing_get_no_env_exits_with_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        with pytest.raises(SystemExit):
            _run("pricing", "get")


class TestRoot:
    def test_no_args_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run()

    def test_unknown_command_exits_with_error(self) -> None:
        with pytest.raises(SystemExit):
            _run("blarg")
