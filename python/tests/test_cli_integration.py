"""DB-backed CLI coverage for operator-facing Bursar workflows."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import psycopg2
import pytest

from bursar import __main__ as cli
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration]

CLI_TENANT_ID = "00000000-0000-4000-8000-000000000101"


def _write_config(path: Path, *, display_name: str) -> None:
    config = deepcopy(CONFIG)
    config["plans"]["pro"]["display_name"] = display_name
    path.write_text(json.dumps(config))


def test_cli_manages_tenants_migrations_and_config_versions(
    pg_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", pg_database_url)
    monkeypatch.setenv("BURSAR_TENANT_ID", CLI_TENANT_ID)

    marker_sql = tmp_path / "marker.sql"
    marker_sql.write_text(
        """
        CREATE TABLE IF NOT EXISTS public.bursar_cli_migrate_marker (
            id integer PRIMARY KEY,
            note text NOT NULL
        );
        INSERT INTO public.bursar_cli_migrate_marker(id, note)
        VALUES (1, 'post-migrate')
        ON CONFLICT (id) DO UPDATE SET note = EXCLUDED.note;
        """
    )
    cli.main(["migrate", "--post-migrate-sql", str(marker_sql)])
    assert "Migrations applied successfully." in capsys.readouterr().out
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT note FROM public.bursar_cli_migrate_marker WHERE id = 1")
        assert cursor.fetchone() == ("post-migrate",)

    cli.main(["tenant", "create", "cli-tenant", "--id", CLI_TENANT_ID, "--display-name", "CLI Tenant"])
    assert capsys.readouterr().out.strip() == CLI_TENANT_ID
    cli.main(["tenant", "status", CLI_TENANT_ID, "suspended"])
    assert capsys.readouterr().out.strip() == CLI_TENANT_ID
    cli.main(["tenant", "status", CLI_TENANT_ID, "active"])
    assert capsys.readouterr().out.strip() == CLI_TENANT_ID

    first_config = tmp_path / "pricing-v1.json"
    second_config = tmp_path / "pricing-v2.json"
    yaml_config = tmp_path / "pricing.yaml"
    _write_config(first_config, display_name="Pro")
    _write_config(second_config, display_name="Pro v2")
    yaml_config.write_text(first_config.read_text())

    cli.main(["config", "validate", str(first_config)])
    assert capsys.readouterr().out == "Bursar config is valid.\n"
    cli.main(["config", "validate", str(yaml_config), "--json"])
    assert json.loads(capsys.readouterr().out) == {"valid": True, "errors": []}

    cli.main(
        [
            "tenant",
            "bootstrap",
            "cli-tenant",
            str(first_config),
            "--id",
            CLI_TENANT_ID,
            "--label",
            "initial",
        ]
    )
    assert f"Tenant {CLI_TENANT_ID} bootstrapped successfully" in capsys.readouterr().out

    cli.main(["config", "get"])
    active = json.loads(capsys.readouterr().out)
    assert active["version"] == 1
    assert active["config"]["plans"]["pro"]["display_name"] == "Pro"

    cli.main(["config", "set", str(second_config), "--label", "updated"])
    assert capsys.readouterr().out == "Bursar config set successfully.\n"
    cli.main(["config", "set", str(second_config), "--label", "unchanged"])
    assert capsys.readouterr().out == "No changes — config is identical to the active version.\n"

    cli.main(["config", "list"])
    listed = capsys.readouterr().out
    assert "* v2" in listed
    assert "  v1" in listed

    cli.main(["config", "export", "1"])
    exported = json.loads(capsys.readouterr().out)
    assert exported["plans"]["pro"]["display_name"] == "Pro"

    cli.main(["config", "diff", "1", "2"])
    diff = capsys.readouterr().out
    assert "--- v1" in diff
    assert "+++ v2" in diff
    assert "Pro v2" in diff

    cli.main(["config", "activate", "1"])
    assert capsys.readouterr().out == "Catalog revision 1 activated.\n"

    with pytest.raises(SystemExit) as missing_export:
        cli.main(["config", "export", "99"])
    assert missing_export.value.code == 1
    assert "Version 99 not found." in capsys.readouterr().err

    with pytest.raises(SystemExit) as bad_tenant:
        cli.main(["tenant", "status", "not-a-uuid", "active"])
    assert bad_tenant.value.code == 1
    assert "Tenant ID must be a UUID" in capsys.readouterr().err

    bad_config = tmp_path / "bad.json"
    bad_config.write_text('{"version": 1, "credits": {"accounting": {"rounding": "invalid"}}}')
    with pytest.raises(SystemExit) as invalid_config:
        cli.main(["config", "validate", str(bad_config), "--json"])
    assert invalid_config.value.code == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
