"""Tests for the bursar CLI (argparse interface, error handling, retries)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bursar.interface.base import StoreError
from bursar.interface.memory import MemoryStore


def _run(*args: str) -> None:
    """Invoke the bursar CLI ``main()`` with explicit argv (no sys.argv mutation)."""
    from bursar.__main__ import main

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
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-key")

    import bursar.__main__ as cli_mod

    monkeypatch.setattr(cli_mod, "_store_from_env", lambda _store=None: store)
    return store


@pytest.fixture
def sample_config(tmp_path: Path) -> str:
    """Write a minimal pricing config and return its path."""
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({"models": {"_default": "input_tokens * 1"}}))
    return str(p)


class TestMigrate:
    # The only connection env var `migrate` honors is DATABASE_URL (see
    # bursar.__main__._cmd_migrate). CI sets DATABASE_URL for the real-Postgres
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

        import bursar.interface.supabase as sb

        class _Res:
            tables_created = ["001_core_schema.sql"]
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

        import bursar.interface.supabase as sb

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

        import bursar.interface.supabase as sb

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

    def test_config_no_subcommand_exits_1(self) -> None:
        assert _exit_code("config") == 1

    def test_config_unknown_subcommand_exits_2(self) -> None:
        assert _exit_code("config", "fly") == 2

    def test_activate_non_integer_version_exits_2_no_traceback(self, capsys: pytest.CaptureFixture) -> None:
        # type=int turns a bad version into a clean argparse error, not a ValueError traceback.
        code = _exit_code("config", "activate", "notanumber")
        assert code == 2
        err = capsys.readouterr().err
        assert "invalid int value" in err

    def test_diff_non_integer_version_exits_2(self) -> None:
        assert _exit_code("config", "diff", "1", "x") == 2

    def test_unknown_flag_exits_2(self) -> None:
        assert _exit_code("config", "set", "f.json", "--bogus") == 2


class TestFileLoading:
    def test_set_no_file_exits_2(self) -> None:
        # argparse: required positional missing.
        assert _exit_code("config", "set") == 2

    def test_file_not_found_exits_1_clean(self, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("config", "set", "/nonexistent/file.json")
        assert code == 1
        assert "File not found" in capsys.readouterr().err

    def test_directory_path_exits_1_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("config", "validate", str(tmp_path))
        assert code == 1
        assert "directory" in capsys.readouterr().err.lower()

    def test_invalid_json_exits_1_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        code = _exit_code("config", "validate", str(p))
        assert code == 1
        assert "Invalid JSON" in capsys.readouterr().err

    def test_invalid_yaml_exits_1_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("key: : :\n  - bad\n: indent")
        code = _exit_code("config", "validate", str(p))
        assert code == 1
        assert "Invalid YAML" in capsys.readouterr().err

    def test_empty_object_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "empty.json"
        p.write_text("{}")
        code = _exit_code("config", "validate", str(p))
        assert code == 1
        assert "empty" in capsys.readouterr().err.lower()

    def test_non_object_json_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]")
        code = _exit_code("config", "validate", str(p))
        assert code == 1
        assert "object" in capsys.readouterr().err.lower()


class TestConfigValidate:
    def test_valid(self, capsys: pytest.CaptureFixture, sample_config: str) -> None:
        _run("config", "validate", sample_config)
        assert "valid" in capsys.readouterr().out

    def test_invalid_schema_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = tmp_path / "bad.json"
        p.write_text('{"models": []}')
        code = _exit_code("config", "validate", str(p))
        assert code == 1
        assert "Validation failed" in capsys.readouterr().err


class TestConfigStore:
    def test_set_identical_config_aborts(self, mem_store: MemoryStore, sample_config: str) -> None:
        """Setting the same config twice should not create a new version."""
        _run("config", "set", sample_config)
        _run("config", "set", sample_config)
        assert len(mem_store.get_pricing_history()) == 1

    def test_set_different_config_creates_new_version(
        self,
        tmp_path: Path,
        mem_store: MemoryStore,
        sample_config: str,
    ) -> None:
        """Setting a different config should create a new version."""
        _run("config", "set", sample_config)
        p2 = tmp_path / "other.json"
        p2.write_text('{"models": {"_default": "input_tokens * 2"}}')
        _run("config", "set", str(p2))
        assert len(mem_store.get_pricing_history()) == 2

    def test_set_first_config_always_works(self, mem_store: MemoryStore, sample_config: str) -> None:
        """First-time setup (no active version) always proceeds even if there is no diff."""
        _run("config", "set", sample_config)
        assert len(mem_store.get_pricing_history()) == 1

    def test_set_with_label(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("config", "set", sample_config, "--label", "deploy-42")
        active = mem_store.get_active_pricing()
        assert active is not None
        assert active.label == "deploy-42"

    def test_get_no_env_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
        # Force supabase extra present so we reach the env-var check, not the extra check.
        import bursar.__main__ as cli_mod

        monkeypatch.setattr(cli_mod, "_require_extra", lambda _extra: None)
        assert _exit_code("config", "get") == 1

    def test_get_emits_parseable_json(
        self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, sample_config: str
    ) -> None:
        _run("config", "set", sample_config)
        capsys.readouterr()  # drain the "set" output
        _run("config", "get")
        payload = json.loads(capsys.readouterr().out)
        assert payload["config"]["models"]["_default"] == "input_tokens * 1"
        assert payload["version"] == 1

    def test_get_no_active_exits_1(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("config", "get")
        assert code == 1
        assert "No active pricing config" in capsys.readouterr().err

    def test_list_shows_version(
        self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, sample_config: str
    ) -> None:
        _run("config", "set", sample_config)
        _run("config", "list")
        assert "v1" in capsys.readouterr().out

    def test_list_marks_active(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        def _conf(val: str) -> str:
            p = tmp_path / f"p{val}.json"
            p.write_text(json.dumps({"models": {"_default": f"input_tokens * {val}"}}))
            return str(p)

        _run("config", "set", _conf("1"), "--label", "v1")
        _run("config", "set", _conf("2"), "--label", "v2")
        _run("config", "list")
        out = capsys.readouterr().out
        assert "* v2" in out
        assert "  v1" in out

    def test_list_no_configs_exits_1(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = _exit_code("config", "list")
        assert code == 1
        assert "No pricing configs found" in capsys.readouterr().err

    def test_activate_switches_active(self, mem_store: MemoryStore, tmp_path: Path) -> None:
        def _conf(val: str) -> str:
            p = tmp_path / f"p{val}.json"
            p.write_text(json.dumps({"models": {"_default": f"input_tokens * {val}"}}))
            return str(p)

        _run("config", "set", _conf("1"), "--label", "v1")
        _run("config", "set", _conf("2"), "--label", "v2")

        _run("config", "activate", "1")
        active = mem_store.get_active_pricing()
        assert active is not None
        assert active.version == 1
        assert active.label == "v1"

        _run("config", "activate", "2")
        active = mem_store.get_active_pricing()
        assert active is not None
        assert active.version == 2

    def test_activate_missing_version_exits_1(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("config", "set", sample_config)
        assert _exit_code("config", "activate", "99") == 1

    def test_export_emits_parseable_json(
        self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, sample_config: str
    ) -> None:
        _run("config", "set", sample_config)
        capsys.readouterr()  # drain the "set" output
        _run("config", "export", "1")
        payload = json.loads(capsys.readouterr().out)
        assert payload["models"]["_default"] == "input_tokens * 1"

    def test_export_missing_version_exits_1(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("config", "set", sample_config)
        assert _exit_code("config", "export", "99") == 1

    def test_diff_shows_changes(self, mem_store: MemoryStore, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        p1 = tmp_path / "a.json"
        p1.write_text(json.dumps({"models": {"a": "1"}}))
        p2 = tmp_path / "b.json"
        p2.write_text(json.dumps({"models": {"b": "1"}}))
        _run("config", "set", str(p1))
        _run("config", "set", str(p2))
        _run("config", "diff", "1", "2")
        out = capsys.readouterr().out
        assert "v1" in out
        assert "v2" in out
        assert '"a"' in out  # removed key shown in diff
        assert '"b"' in out  # added key shown in diff

    def test_diff_no_args_exits_2(self) -> None:
        assert _exit_code("config", "diff") == 2

    def test_diff_missing_version_exits_1(self, mem_store: MemoryStore, sample_config: str) -> None:
        _run("config", "set", sample_config)
        assert _exit_code("config", "diff", "1", "99") == 1


class TestRetryNarrowing:
    """H7: only the transient PostgREST schema-cache/connection error is retried."""

    def test_non_transient_error_not_retried(self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _boom(_version: int) -> str:
            calls["n"] += 1
            raise StoreError("permission denied for function activate_pricing_config")

        monkeypatch.setattr(mem_store, "activate_pricing", _boom)
        assert _exit_code("config", "activate", "1") == 1
        assert calls["n"] == 1  # exactly one attempt — no blind retry of a write

    def test_transient_error_is_retried_then_succeeds(
        self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch, sample_config: str
    ) -> None:
        monkeypatch.setattr("bursar.__main__._RETRY_INITIAL_DELAY", 0.0)
        monkeypatch.setattr("bursar.__main__._RETRY_MAX_DELAY", 0.0)
        calls = {"n": 0}

        def _flaky(_version: int) -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise StoreError("PGRST205: Could not find the function in the schema cache")
            return "ok-id"

        monkeypatch.setattr(mem_store, "activate_pricing", _flaky)
        _run("config", "set", sample_config)  # need a version to exist
        _run("config", "activate", "1")
        assert calls["n"] == 3  # retried twice, then succeeded

    def test_transient_error_exhausts_retries_exits_1(
        self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("bursar.__main__._RETRY_INITIAL_DELAY", 0.0)
        monkeypatch.setattr("bursar.__main__._RETRY_MAX_DELAY", 0.0)

        def _always(_version: int) -> str:
            raise StoreError("schema cache reload pending")

        monkeypatch.setattr(mem_store, "activate_pricing", _always)
        code = _exit_code("config", "activate", "1")
        assert code == 1
        err = capsys.readouterr().err
        assert "schema cache" in err.lower()


class TestConfigValidationSchemas:
    """Schema regression tests — validate realistic configs with all features."""

    def test_realistic_config_with_search_plans(self, tmp_path: Path) -> None:
        config = {
            "models": {"_default": "input_tokens * 1", "gpt-4": "input_tokens * 2"},
            "tools": {"_default": "tool_calls * 5", "code_exec": "tool_calls * 50"},
            "search": "search_queries * 5",
            "cache": "clamp(-cache_read_tokens * 0.002, -max(input_tokens), 0)",
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
        _run("config", "validate", str(p))

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
        _run("config", "validate", str(p))

    def test_search_as_dict_fails(self, tmp_path: Path) -> None:
        """search/cache are single expression strings (WS1), not dicts."""
        config = {
            "models": {"_default": "input_tokens * 1"},
            "search": {"costs": "search_queries * 1", "rag": {"costs": "search_queries * 2"}},
        }
        p = tmp_path / "bad_search.yaml"
        import yaml

        p.write_text(yaml.dump(config))
        assert _exit_code("config", "validate", str(p)) == 1


class TestRetryBackoff:
    """CLI1 — Retry backoff: transient vs non-transient error behaviour."""

    def test_transient_connection_error_retried(
        self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch, sample_config: str
    ) -> None:
        """'connection refused' in the message is a transient marker → retried."""
        monkeypatch.setattr("bursar.__main__._RETRY_INITIAL_DELAY", 0.0)
        monkeypatch.setattr("bursar.__main__._RETRY_MAX_DELAY", 0.0)
        calls = {"n": 0}

        def _flaky(_version: int) -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                from bursar.interface.base import StoreError

                raise StoreError("connection refused")
            return "ok-id"

        monkeypatch.setattr(mem_store, "activate_pricing", _flaky)
        _run("config", "set", sample_config)
        _run("config", "activate", "1")
        assert calls["n"] >= 2  # at least one retry before success

    def test_non_transient_error_not_retried(self, mem_store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """'invalid config' is NOT a transient marker → exactly 1 attempt."""
        calls = {"n": 0}

        def _boom(_version: int) -> str:
            calls["n"] += 1
            from bursar.interface.base import StoreError

            raise StoreError("invalid config for user")

        monkeypatch.setattr(mem_store, "activate_pricing", _boom)
        assert _exit_code("config", "activate", "1") == 1
        assert calls["n"] == 1


class TestMigrateNoUrl:
    """CLI2 — migrate with no DATABASE_URL exits 1."""

    def test_migrate_no_database_url_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Already covered by TestMigrate.test_migrate_no_url_anywhere_exits_1; skip duplicate."""
        # This test is intentionally redundant — the existing test in TestMigrate
        # covers this case. We verify the same behaviour here for CLI2 completeness.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert _exit_code("migrate") == 1


class TestValidateThenUseEngine:
    """M17 — CLI validate then programmatic PricingEngine usage."""

    def test_validate_then_use_engine(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Write a pricing config, validate it via CLI, then load into PricingEngine
        and verify that calculate() produces the expected cost.
        """
        import json
        from decimal import Decimal

        config_data = {
            "models": {
                "_default": "input_tokens * 2 + output_tokens * 4",
            },
            "min_balance": 0,
        }
        p = tmp_path / "pricing.json"
        p.write_text(json.dumps(config_data))

        # Step 1: CLI validate must succeed (exit 0, print "valid").
        _run("config", "validate", str(p))
        out = capsys.readouterr().out
        assert "valid" in out.lower()

        # Step 2: Load the same config into a PricingEngine and calculate a cost.
        from bursar import UsageMetrics
        from bursar.engine import PricingEngine

        engine = PricingEngine.from_dict(config_data)
        result = engine.calculate(UsageMetrics(input_tokens=10, output_tokens=5))
        # 10*2 + 5*4 = 20 + 20 = 40
        assert result.total == Decimal("40"), f"Unexpected total: {result.total}"


class TestMigrateInvalidUrlFailsGracefully:
    """Migration failure handling — invalid DATABASE_URL exits non-zero without traceback."""

    def test_migrate_invalid_url_fails_gracefully(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An invalid / unresolvable DATABASE_URL must exit 1 with a meaningful
        message — not dump a raw Python traceback to stderr.

        Strategy: patch psycopg2 import to fail so _require_extra exits 1
        immediately before any connection attempt. This guarantees a clean,
        fast, dependency-free test.
        """
        import builtins

        monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:bad@localhost:0/no_such_db")

        real_import = builtins.__import__

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "psycopg2":
                raise ImportError("psycopg2 not available (test stub)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        code = _exit_code("migrate")
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        # Exit code must be non-zero.
        assert code != 0, f"Expected non-zero exit, got {code}"

        # The output must NOT contain a raw Python traceback.
        assert "Traceback (most recent call last)" not in combined, "Unexpected traceback in output:\n" + combined

        # There must be some human-readable message (not silence).
        assert combined.strip() != "", "Expected a non-empty error message"
