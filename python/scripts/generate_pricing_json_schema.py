#!/usr/bin/env python3
"""Generate the checked-in pricing JSON Schema from BursarConfig."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bursar.config.types import public_config_json_schema

_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ROOT.parent
_SQL_DIR = _ROOT / "src" / "bursar" / "sql"
OUTPUT = _REPO_ROOT / "docs" / "pricing-config.schema.json"
JAVASCRIPT_OUTPUT = _REPO_ROOT / "javascript" / "src" / "generated" / "pricing-config.schema.json"
GO_OUTPUT = _REPO_ROOT / "golang" / "internal" / "config" / "pricing-config.schema.json"
SQL_SCHEMA_BEGIN = "-- BEGIN GENERATED CATALOG SHAPE SCHEMA"
SQL_SCHEMA_END = "-- END GENERATED CATALOG SHAPE SCHEMA"

_SHAPE_KEYS = {
    "$ref",
    "additionalProperties",
    "anyOf",
    "const",
    "enum",
    "items",
    "minProperties",
    "oneOf",
    "propertyNames",
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
CREATE FUNCTION bursar.catalog_document_shape_schema()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $catalog_json${payload}$catalog_json$::jsonb
$function$;
{SQL_SCHEMA_END}"""


def main() -> None:
    schema = public_config_json_schema()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(schema, indent=2) + "\n"
    OUTPUT.write_text(rendered, encoding="utf-8")
    JAVASCRIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    JAVASCRIPT_OUTPUT.write_text(rendered, encoding="utf-8")
    GO_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    GO_OUTPUT.write_text(rendered, encoding="utf-8")

    migration = _SQL_DIR / "001_schema_and_types.sql"
    sql = migration.read_text(encoding="utf-8")
    start = sql.index(SQL_SCHEMA_BEGIN)
    end = sql.index(SQL_SCHEMA_END, start) + len(SQL_SCHEMA_END)
    migration.write_text(sql[:start] + render_sql_shape_schema(schema) + sql[end:], encoding="utf-8")

    sys.stdout.write(f"Wrote {OUTPUT}, {JAVASCRIPT_OUTPUT}, and {GO_OUTPUT}\n")


if __name__ == "__main__":
    main()
