"""CLI parsing and failure-mode tests that never require a database."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bursar import __main__ as cli
from bursar.credits.store import StoreError


def test_load_pricing_file_supports_json_yaml_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path = tmp_path / "pricing.json"
    json_path.write_text(
        json.dumps(
            {
                "version": 1,
                "credits": {
                    "accounting": {
                        "unit": "credit",
                        "scale": 6,
                        "rounding": "half_up",
                    }
                },
            }
        )
    )
    assert cli._load_pricing_file(str(json_path))["version"] == 1

    yaml_path = tmp_path / "pricing.yaml"
    yaml_path.write_text("version: 1\ncredits:\n  accounting:\n    unit: credit\n    scale: 6\n    rounding: half_up\n")
    assert cli._load_pricing_file(str(yaml_path))["version"] == 1

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"version": 1}'))
    assert cli._load_pricing_file("-")["version"] == 1


@pytest.mark.parametrize(
    "raw, message", [("{", "Invalid JSON"), ("[]", "must be a JSON/YAML object"), ("{}", "is empty")]
)
def test_load_pricing_file_reports_clean_errors(
    tmp_path: Path, raw: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(raw)
    with pytest.raises(SystemExit) as exc:
        cli._load_pricing_file(str(path))
    assert exc.value.code == 1
    assert message in capsys.readouterr().err


def test_parser_requires_a_command_and_config_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["config"])


def test_migrate_accepts_ordered_post_migration_sql_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first.write_text("SELECT 1;\n")
    second.write_text("SELECT 2;\n")
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def run_migrations(
        database_url: str,
        *,
        post_migration_sql: list[tuple[str, str]],
    ) -> None:
        calls.append((database_url, post_migration_sql))

    monkeypatch.setenv("DATABASE_URL", "postgresql://example.test/bursar")
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr("bursar.credits.postgres.store.run_migrations", run_migrations)

    args = SimpleNamespace(post_migrate_sql=[str(first), str(second)])
    cli._cmd_migrate(args)

    assert calls == [
        (
            "postgresql://example.test/bursar",
            [(str(first), "SELECT 1;\n"), (str(second), "SELECT 2;\n")],
        )
    ]
    assert capsys.readouterr().out == "Migrations applied successfully.\n"


@pytest.mark.parametrize("contents", [None, " \n"])
def test_migrate_rejects_unreadable_or_empty_post_migration_sql(
    tmp_path: Path,
    contents: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "host.sql"
    if contents is not None:
        path.write_text(contents)
    called = False

    def run_migrations(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setenv("DATABASE_URL", "postgresql://example.test/bursar")
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr("bursar.credits.postgres.store.run_migrations", run_migrations)

    with pytest.raises(SystemExit) as exc:
        cli._cmd_migrate(SimpleNamespace(post_migrate_sql=[str(path)]))

    assert exc.value.code == 1
    assert not called
    assert str(path) in capsys.readouterr().err


def test_retry_transient_retries_only_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def transient() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StoreError("PGRST205 schema cache")
        return "ok"

    monkeypatch.setattr(cli, "_RETRY_INITIAL_DELAY", 0)
    assert cli._retry_transient(transient, what="test") == "ok"
    assert attempts == 3

    def permanent() -> None:
        raise StoreError("permission denied")

    with pytest.raises(SystemExit):
        cli._retry_transient(permanent, what="test")
