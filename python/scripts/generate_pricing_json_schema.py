#!/usr/bin/env python3
"""Generate the checked-in pricing JSON Schema from BursarConfig."""

from __future__ import annotations

import json
from pathlib import Path

from bursar.config import BursarConfig

_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ROOT.parent
OUTPUT = _REPO_ROOT / "docs" / "pricing-config.schema.json"
JAVASCRIPT_OUTPUT = _REPO_ROOT / "javascript" / "src" / "generated" / "pricing-config.schema.json"
SQL_OUTPUT = _ROOT / "src" / "bursar" / "sql" / "001_schema_and_types.sql"
SQL_SCHEMA_BEGIN = "-- BEGIN GENERATED CATALOG SHAPE SCHEMA"
SQL_SCHEMA_END = "-- END GENERATED CATALOG SHAPE SCHEMA"

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


def replace_sql_shape_schema(schema: dict[str, object]) -> None:
    source = SQL_OUTPUT.read_text(encoding="utf-8")
    start = source.index(SQL_SCHEMA_BEGIN)
    end = source.index(SQL_SCHEMA_END, start) + len(SQL_SCHEMA_END)
    rendered = source[:start] + render_sql_shape_schema(schema) + source[end:]
    SQL_OUTPUT.write_text(rendered, encoding="utf-8")


def main() -> None:
    schema = BursarConfig.model_json_schema()
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    schema.setdefault("$id", "https://zonastery.github.io/bursar/pricing-config.schema.json")
    schema.setdefault(
        "$comment",
        "Structural schema. Run `bursar config validate` for cross-reference and pricing semantics.",
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(schema, indent=2) + "\n"
    OUTPUT.write_text(rendered, encoding="utf-8")
    JAVASCRIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    JAVASCRIPT_OUTPUT.write_text(rendered, encoding="utf-8")
    replace_sql_shape_schema(schema)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
