"""The checked-in JSON Schema must match the Pydantic source model."""

from __future__ import annotations

import json
import re
from pathlib import Path

from bursar.config.types import public_config_json_schema


def _compact_shape_schema(value: object) -> object:
    keys = {
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
    if isinstance(value, list):
        return [_compact_shape_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            {name: _compact_shape_schema(schema) for name, schema in item.items()}
            if key in {"$defs", "properties"} and isinstance(item, dict)
            else _compact_shape_schema(item)
        )
        for key, item in value.items()
        if key in keys or key in {"$defs", "properties"}
    }


def test_generated_json_schema_is_current() -> None:
    repository = Path(__file__).resolve().parents[2]
    expected = public_config_json_schema()

    outputs = [
        repository / "docs" / "pricing-config.schema.json",
        repository / "javascript" / "src" / "generated" / "pricing-config.schema.json",
        repository / "golang" / "internal" / "config" / "pricing-config.schema.json",
    ]
    for output in outputs:
        assert json.loads(output.read_text(encoding="utf-8")) == expected

    sql_dir = repository / "python" / "src" / "bursar" / "sql"
    definitions: list[tuple[Path, str]] = []
    for migration in sorted(sql_dir.glob("[0-9][0-9][0-9]_*.sql")):
        sql = migration.read_text(encoding="utf-8")
        for function in re.finditer(
            r"CREATE(?: OR REPLACE)? FUNCTION\s+bursar\.catalog_document_shape_schema\(\)(.*?)\$function\$;",
            sql,
            re.DOTALL,
        ):
            definitions.append((migration, function.group(1)))
    assert len(definitions) == 1
    migration, function_body = definitions[0]
    assert migration.name == "001_schema_and_types.sql"
    generated = re.search(r"SELECT\s+\$catalog_json\$(.*?)\$catalog_json\$::jsonb", function_body, re.DOTALL)
    assert generated is not None
    assert json.loads(generated.group(1)) == _compact_shape_schema(expected)


def test_public_schema_matches_cli(capsys) -> None:
    from bursar import __main__ as cli

    repository = Path(__file__).resolve().parents[2]
    artifact = repository / "docs" / "pricing-config.schema.json"
    cli.main(["config", "schema"])
    actual = json.loads(capsys.readouterr().out)
    assert actual == json.loads(artifact.read_text(encoding="utf-8"))
    assert actual["properties"]["version"]["const"] == 1
