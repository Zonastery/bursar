#!/usr/bin/env python3
"""Generate the checked-in pricing JSON Schema from BursarConfig."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bursar.config import BursarConfig

_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ROOT.parent
_SQL_DIR = _ROOT / "src" / "bursar" / "sql"
OUTPUT = _REPO_ROOT / "docs" / "pricing-config.schema.json"
JAVASCRIPT_OUTPUT = _REPO_ROOT / "javascript" / "src" / "generated" / "pricing-config.schema.json"
GO_OUTPUT = _REPO_ROOT / "golang" / "internal" / "config" / "pricing-config.schema.json"
SQL_SCHEMA_BEGIN = "-- BEGIN GENERATED CATALOG SHAPE SCHEMA"
SQL_SCHEMA_END = "-- END GENERATED CATALOG SHAPE SCHEMA"
_MIGRATION_NAME = re.compile(r"^(?P<prefix>[0-9]{3})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")

_SHAPE_KEYS = {
    "$ref",
    "additionalProperties",
    "anyOf",
    "const",
    "enum",
    "items",
    "oneOf",
    "required",
    "type",
}


def compact_shape_schema(value: object) -> object:
    """Keep only JSON Schema keywords enforced by the SQL shape validator."""

    if isinstance(value, list):
        return [compact_shape_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    compact: dict[str, object] = {}
    for key, item in value.items():
        if key in {"$defs", "properties"} and isinstance(item, dict):
            compact[key] = {name: compact_shape_schema(schema) for name, schema in item.items()}
        elif key in _SHAPE_KEYS:
            compact[key] = compact_shape_schema(item)
    return compact


def render_sql_shape_schema(schema: dict[str, object]) -> str:
    payload = json.dumps(compact_shape_schema(schema), separators=(",", ":"))
    return f"""\
{SQL_SCHEMA_BEGIN}
-- Embed the generated catalog shape contract in PostgreSQL so write-boundary
-- and projection validators share structural definitions with the SDKs.
CREATE OR REPLACE FUNCTION bursar.catalog_document_shape_schema()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $catalog_json${payload}$catalog_json$::jsonb
$function$;
{SQL_SCHEMA_END}"""


def migration_path(filename: str) -> tuple[Path, str]:
    """Validate a new migration path and return it with its predecessor."""

    if Path(filename).name != filename:
        raise ValueError("--sql-migration must be a filename, not a path")
    match = _MIGRATION_NAME.fullmatch(filename)
    if match is None:
        raise ValueError("--sql-migration must use the NNN_snake_case.sql format")

    files = sorted(_SQL_DIR.glob("*.sql"), key=lambda path: path.name)
    expected_prefix = len(files) + 1
    if int(match.group("prefix")) != expected_prefix:
        raise ValueError(f"next migration prefix must be {expected_prefix:03d}")

    output = _SQL_DIR / filename
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing migration: {output}")
    return output, files[-1].name


def render_sql_migration(schema: dict[str, object], filename: str, predecessor: str) -> str:
    return f"""-- Migration: {filename}
-- Purpose: Refresh the generated catalog shape schema used by PostgreSQL validators.
-- Depends on: {predecessor} and catalog_document_shape_schema() from 001_schema_and_types.sql.
-- Security: Replaces the immutable schema helper while preserving its owner and grants.

{render_sql_shape_schema(schema)}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sql-migration",
        metavar="NNN_name.sql",
        help="append a new immutable migration for the generated PostgreSQL catalog shape schema",
    )
    args = parser.parse_args()

    schema = BursarConfig.model_json_schema()
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    schema.setdefault("$id", "https://zonastery.github.io/bursar/pricing-config.schema.json")
    schema.setdefault(
        "$comment",
        "Structural schema. Run `bursar config validate` for cross-reference and pricing semantics.",
    )
    migration: tuple[Path, str, str] | None = None
    if args.sql_migration:
        output, predecessor = migration_path(args.sql_migration)
        migration = (output, predecessor, args.sql_migration)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(schema, indent=2) + "\n"
    OUTPUT.write_text(rendered, encoding="utf-8")
    JAVASCRIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    JAVASCRIPT_OUTPUT.write_text(rendered, encoding="utf-8")
    GO_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    GO_OUTPUT.write_text(rendered, encoding="utf-8")

    if migration is not None:
        output, predecessor, filename = migration
        output.write_text(render_sql_migration(schema, filename, predecessor), encoding="utf-8")
        sys.stdout.write(f"Wrote {output}\n")

    sys.stdout.write(f"Wrote {OUTPUT}, {JAVASCRIPT_OUTPUT}, and {GO_OUTPUT}\n")


if __name__ == "__main__":
    main()
