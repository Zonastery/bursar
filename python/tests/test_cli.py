"""Tests for the ducto CLI (argparse interface, error handling, retries)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ducto.interface.base import StoreError
from ducto.interface.memory import MemoryStore


def _run(*args: str) -> None:
    """Invoke the ducto CLI ``main()`` with explicit argv (no sys.argv mutation)."""
    from ducto.__main__ import main

    main(list(args))


def _exit_code(*args: str) -> int:
    """Run the CLI and return its exit code (0 on clean return)."""
    with pytest.raises(SystemExit) as exc:
        _run(*args)
    code = exc.value.code
    return 0 if code is None else int(code)  # type: ignore[arg-type]


@pytest.fixture
def mem_store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    """Replace _store_from_env with an in-memory store."""
    store = MemoryStore()
    monkeypatch.setenv("SUPABASE_URL", "http://localhost")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")

    import ducto.__main__ as cli_mod

    monkeypatch.setattr(cli_mod, "_store_from_env", lambda: store)
    return store


@pytest.fixture
def sample_config(tmp_path: Path) -> str:
    """Write a minimal pricing config and return its path."""
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({"models": {"_default": "input_tokens * 1"}}))
    return str(p)


class TestMigrate:
    # The only connection env var `migrate` honors is DATABASE_URL (see
    # ducto.__main__._cmd_migrate). CI sets DATABASE_URL for the real-Postgres
    # integration tests, so these tests must control it explicitly via
    # monkeypatch rather than relying on the ambient environment — otherwise
    # the "no config → error" cases pass locally but fail in CI.

    def test_migrate_no_url_anywhere_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deterministically clear the connection env var so this errors with
        # exit 1 regardless of whether CI exported DATABASE_URL.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert _exit_code("migrate") == 1

    def test_migrate_reads_database_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env var is the primary mechanism; run_migrations gets it, not argv."""
        # _require_extra needs psycopg2 importable; skip if not present.
        pytest.importorskip("psycopg2")
        captured: dict[str, str] = {}

        import ducto.interface.supabase as sb

        class _Res:
            tables_created = ["001_credit_tables.sql"]
            errors: list[str] = []
            success = True

        def _fake(url: str) -> _Res:
            captured["url"] = url
            return _Res()

        monkeypatch.setattr(sb, "run_migrations", _fake)
        monkeypatch.setattr("builtins.__import__", __import__)  # keep _require_extra happy
        # Pin DATABASE_URL to a known value so the assertion holds regardless of
        # any DATABASE_URL exported by CI for the integration tests.
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
        _run("migrate")
        assert captured["url"] == "postgresql://u:p@h/db"

    def test_migrate_positional_warns(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        pytest.importorskip("psycopg2")
        # Clear the env so the positional URL is the only source — otherwise an
        # ambient (CI) DATABASE_URL would win and the positional path goes
        # untested.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        captured: dict[str, str] = {}

        import ducto.interface.supabase as sb

        class _Res:
            tables_created: list[str] = []
            errors: list[str] = []
            success = True

        monkeypatch.setattr(sb, "run_migrations", lambda url: (captured.update(url=url), _Res())[1])
        _run("migrate", "postgresql://u:p@h/db")
        err = capsys.readouterr().err
        assert "warning" in err.lower()
        assert "leak" in err.lower()
        assert captured["url"] == "postgresql://u:p@h/db"

    def test_migrate_env_wins_over_positional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("psycopg2")
        captured: dict[str, str] = {}

        import ducto.interface.supabase as sb

        class _Res:
            tables_created: list[str] = []
            errors: list[str] = []
            success = True

        monkeypatch.setattr(sb, "run_migrations", lambda url: (captured.update(url=url), _Res())[1])
        # Pin a sentinel env URL (overriding any ambient/CI DATABASE_URL) and
        # assert it beats the positional one.
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
        _run("migrate", "postgresql://cli/db")
        assert captured["url"] == "postgresql://env/db"


class TestArgparse:
    def test_root_help_exits_0(self) -> None:
        assert _exit_code("--help") == 0

    def test_no_args_exits_1(self) -> None:
        assert _exit_code() == 1

    def test_unknown_command_exits_2(self) -> None:
        assert _exit_code("blarg") == 2

    def test_pricing_no_subcommand_exits_1(self) -> None:
        assert _exit_code("pricing") == 1

    def test_pricing_unknown_subcommand_exits_2(self) -> None:
        assert _exit_code("pricing", "fly") == 2

    def test_activate_non_integer_version_exits_2_no_traceback(self, capsys: pytest.CaptureFixture) -> None:
        # type=int turns a bad version into a clean argparse error, not a ValueError traceback.
        code = _exit_code("pricing", "activate", "notanumber")
        assert code == 2
        err = capsys.readouterr().err
        assert "invalid int value" in err

    def test_diff_non_integer_version_exits_2(self) -> None:
        assert _exit_code("pricing", "diff", "1", "x") == 2

    def test_unknown_flag_exits_2(self) -> None:
        assert _exit_code("pricing", "set", "f.json", "--bogus") == 2


class TestFileLoading:
    def test_set_no_file_exits_2(self) -> None:
        # argparse: required positional missing.
        assert _exit_code("pricing", "set") == 2

    def test_file_not_found_exits_1_clean(self, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("pricing", "set", "/nonexistent/file.json")
        assert code == 1
        assert "File not found" in capsys.readouterr().err

    def test_directory_path_exits_1_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("pricing", "validate", str(tmp_path))
        assert code == 1
        assert "directory" in capsys.readouterr().err.lower()

    def test_invalid_json_exits_1_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        code = _exit_code("pricing", "validate", str(p))
        assert code == 1
        assert "Invalid JSON" in capsys.readouterr().err

    def test_invalid_yaml_exits_1_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("key: : :\n  - bad\n: indent")
        code = _exit_code("pricing", "validate", str(p))
        assert code == 1
        assert "Invalid YAML" in capsys.readouterr().err

    def test_empty_object_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "empty.json"
        p.write_text("{}")
        code = _exit_code("pricing", "validate", str(p))
        assert code == 1
        assert "empty" in capsys.readouterr().err.lower()

    def test_non_object_json_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]")
        code = _exit_code("pricing", "validate", str(p))
        assert code == 1
        assert "object" in capsys.readouterr().err.lower()


class TestPricingValidate:
    def test_valid(self, capsys: pytest.CaptureFixture, sample_config: str) -> None:
        _run("pricing", "validate", sample_config)
        assert "valid" in capsys.readouterr().out

    def test_invalid_schema_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "bad.json"
        p.write_text('{"models": []}')
        code = _exit_code("pricing", "validate", str(p))
        assert code == 1
        assert "Validation failed" in capsys.readouterr().err


class TestPricingStore:
    def test_set_always_creates_new_version(self, mem_store: MemoryStore, sample_config: str) -> None:
        for _ in range(3):
            _run("pricing", "set", sample_config)
        assert len(mem_store.get_pricing_history()) == 3

    def test_set_with_label(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("pricing", "set", sample_config, "--label", "deploy-42")
        active = mem_store.get_active_pricing()
        assert active is not None
        assert active.label == "deploy-42"

    def test_get_no_env_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        # Force supabase extra present so we reach the env-var check, not the extra check.
        import ducto.__main__ as cli_mod

        monkeypatch.setattr(cli_mod, "_require_extra", lambda _extra: None)
        assert _exit_code("pricing", "get") == 1

    def test_get_emits_parseable_json(
        self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, sample_config: str
    ) -> None:
        _run("pricing", "set", sample_config)
        capsys.readouterr()  # drain the "set" output
        _run("pricing", "get")
        payload = json.loads(capsys.readouterr().out)
        assert payload["config"]["models"]["_default"] == "input_tokens * 1"
        assert payload["version"] == 1

    def test_get_no_active_exits_1(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("pricing", "get")
        assert code == 1
        assert "No active pricing config" in capsys.readouterr().err

    def test_list_shows_version(
        self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, sample_config: str
    ) -> None:
        _run("pricing", "set", sample_config)
        _run("pricing", "list")
        assert "v1" in capsys.readouterr().out

    def test_list_marks_active(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        def _conf(val: str) -> str:
            p = tmp_path / f"p{val}.json"
            p.write_text(json.dumps({"models": {"_default": f"input_tokens * {val}"}}))
            return str(p)

        _run("pricing", "set", _conf("1"), "--label", "v1")
        _run("pricing", "set", _conf("2"), "--label", "v2")
        _run("pricing", "list")
        out = capsys.readouterr().out
        assert "* v2" in out
        assert "  v1" in out

    def test_list_no_configs_exits_1(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("pricing", "list")
        assert code == 1
        assert "No pricing configs found" in capsys.readouterr().err

    def test_activate_switches_active(self, mem_store: MemoryStore, tmp_path: Path) -> None:
        def _conf(val: str) -> str:
            p = tmp_path / f"p{val}.json"
            p.write_text(json.dumps({"models": {"_default": f"input_tokens * {val}"}}))
            return str(p)

        _run("pricing", "set", _conf("1"), "--label", "v1")
        _run("pricing", "set", _conf("2"), "--label", "v2")

        _run("pricing", "activate", "1")
        active = mem_store.get_active_pricing()
        assert active is not None
        assert active.version == 1
        assert active.label == "v1"

        _run("pricing", "activate", "2")
        active = mem_store.get_active_pricing()
        assert active is not None
        assert active.version == 2

    def test_activate_missing_version_exits_1(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("pricing", "set", sample_config)
        assert _exit_code("pricing", "activate", "99") == 1

    def test_export_emits_parseable_json(
        self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, sample_config: str
    ) -> None:
        _run("pricing", "set", sample_config)
        capsys.readouterr()  # drain the "set" output
        _run("pricing", "export", "1")
        payload = json.loads(capsys.readouterr().out)
        assert payload["models"]["_default"] == "input_tokens * 1"

    def test_export_missing_version_exits_1(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("pricing", "set", sample_config)
        assert _exit_code("pricing", "export", "99") == 1

    def test_diff_shows_changes(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        p1 = tmp_path / "a.json"
        p1.write_text(json.dumps({"models": {"a": "1"}}))
        p2 = tmp_path / "b.json"
        p2.write_text(json.dumps({"models": {"b": "1"}}))
        _run("pricing", "set", str(p1))
        _run("pricing", "set", str(p2))
        _run("pricing", "diff", "1", "2")
        out = capsys.readouterr().out
        assert "v1" in out
        assert "v2" in out
        assert '"a"' in out  # removed key shown in diff
        assert '"b"' in out  # added key shown in diff

    def test_diff_no_args_exits_2(self) -> None:
        assert _exit_code("pricing", "diff") == 2

    def test_diff_missing_version_exits_1(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("pricing", "set", sample_config)
        assert _exit_code("pricing", "diff", "1", "99") == 1


class TestRetryNarrowing:
    """H7: only the transient PostgREST schema-cache/connection error is retried."""

    def test_non_transient_error_not_retried(self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _boom(_version: int) -> str:
            calls["n"] += 1
            raise StoreError("permission denied for function activate_pricing_config")

        monkeypatch.setattr(mem_store, "activate_pricing", _boom)
        assert _exit_code("pricing", "activate", "1") == 1
        assert calls["n"] == 1  # exactly one attempt — no blind retry of a write

    def test_transient_error_is_retried_then_succeeds(
        self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch, sample_config: str
    ) -> None:
        monkeypatch.setattr("ducto.__main__._RETRY_INITIAL_DELAY", 0.0)
        monkeypatch.setattr("ducto.__main__._RETRY_MAX_DELAY", 0.0)
        calls = {"n": 0}

        def _flaky(_version: int) -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise StoreError("PGRST205: Could not find the function in the schema cache")
            return "ok-id"

        monkeypatch.setattr(mem_store, "activate_pricing", _flaky)
        _run("pricing", "set", sample_config)  # need a version to exist
        _run("pricing", "activate", "1")
        assert calls["n"] == 3  # retried twice, then succeeded

    def test_transient_error_exhausts_retries_exits_1(
        self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("ducto.__main__._RETRY_INITIAL_DELAY", 0.0)
        monkeypatch.setattr("ducto.__main__._RETRY_MAX_DELAY", 0.0)

        def _always(_version: int) -> str:
            raise StoreError("schema cache reload pending")

        monkeypatch.setattr(mem_store, "activate_pricing", _always)
        code = _exit_code("pricing", "activate", "1")
        assert code == 1
        err = capsys.readouterr().err
        assert "schema cache" in err.lower()


class TestPricingValidationSchemas:
    """Schema regression tests — validate realistic configs with all features."""

    def test_realistic_config_with_search_plans(self, tmp_path: Path) -> None:
        config = {
            "models": {"_default": "input_tokens * 1", "gpt-4": "input_tokens * 2"},
            "tools": {"_default": "tool_calls * 5", "code_exec": "tool_calls * 50"},
            "search": {"costs": "search_queries * 5", "rag": "search_queries * 10"},
            "cache": {"discount": "clamp(-cache_read_tokens * 0.002, -max(input_tokens), 0)"},
            "fixed": {"batch": 100, "roadmap_gen": 20000},
            "min_balance": 5000,
            "plans": {
                "free": {
                    "id": "free",
                    "name": "Free",
                    "free_allowance": 5000,
                    "features": {"max_daily_roadmaps": 1, "max_concurrency": 1},
                },
                "pro": {
                    "id": "pro",
                    "name": "Pro",
                    "free_allowance": 50000,
                    "features": {"max_daily_roadmaps": 10, "max_concurrency": 3},
                },
            },
        }
        p = tmp_path / "realistic.yaml"
        import yaml

        p.write_text(yaml.dump(config))
        _run("pricing", "validate", str(p))

    def test_features_bool_and_int(self, tmp_path: Path) -> None:
        config = {
            "models": {"_default": "input_tokens * 1"},
            "plans": {
                "tier1": {
                    "id": "tier1",
                    "name": "Tier 1",
                    "features": {"premium": True, "max_items": 100},
                },
            },
        }
        p = tmp_path / "features.yaml"
        import yaml

        p.write_text(yaml.dump(config))
        _run("pricing", "validate", str(p))

    def test_mixed_search_nested_dict_fails(self, tmp_path: Path) -> None:
        config = {
            "models": {"_default": "input_tokens * 1"},
            "search": {"costs": "search_queries * 1", "rag": {"costs": "search_queries * 2"}},
        }
        p = tmp_path / "bad_search.yaml"
        import yaml

        p.write_text(yaml.dump(config))
        assert _exit_code("pricing", "validate", str(p)) == 1
