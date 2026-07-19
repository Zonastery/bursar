"""CLI parsing and failure-mode tests that never require a database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bursar import __main__ as cli
from bursar.interface.base import StoreError


def test_load_pricing_file_supports_json_yaml_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path = tmp_path / "pricing.json"
    json_path.write_text(json.dumps({"version": 1, "metering": {"models": {"*": "input_tokens"}}}))
    assert cli._load_pricing_file(str(json_path))["version"] == 1

    yaml_path = tmp_path / "pricing.yaml"
    yaml_path.write_text("version: 1\nmetering:\n  models:\n    '*': input_tokens\n")
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
