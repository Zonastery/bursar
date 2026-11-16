"""The checked-in JSON Schema must match the Pydantic source model."""

from __future__ import annotations

import json
import re
from pathlib import Path

from bursar.config import BursarConfig


def _compact_shape_schema(value: object) -> object:
    keys = {
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
    expected = BursarConfig.model_json_schema()
    expected.setdefault(
        "$schema",
        "https://json-schema.org/draft/2020-12/schema",
    )
    expected.setdefault(
        "$id",
        "https://zonastery.github.io/bursar/pricing-config.schema.json",
    )
    expected.setdefault(
        "$comment",
        "Structural schema. Run `bursar config validate` for cross-reference and pricing semantics.",
    )

    outputs = [
        repository / "docs" / "pricing-config.schema.json",
        repository / "javascript" / "src" / "generated" / "pricing-config.schema.json",
        repository / "golang" / "internal" / "config" / "pricing-config.schema.json",
    ]
    for output in outputs:
        assert json.loads(output.read_text(encoding="utf-8")) == expected

    # Migrations are checksummed and immutable, so validate the latest function definition.
    sql_dir = repository / "python" / "src" / "bursar" / "sql"
    latest_sql_schema: str | None = None
    for migration in sorted(sql_dir.glob("[0-9][0-9][0-9]_*.sql")):
        sql = migration.read_text(encoding="utf-8")
        function = re.search(
            r"CREATE(?: OR REPLACE)? FUNCTION\s+bursar\.catalog_document_shape_schema\(\)(.*?)\$function\$;",
            sql,
            re.DOTALL,
        )
        if function is None:
            continue
        generated = re.search(
            r"SELECT\s+\$catalog_json\$(.*?)\$catalog_json\$::jsonb",
            function.group(1),
            re.DOTALL,
        )
        assert generated is not None, f"{migration}: catalog schema function has no generated JSON payload"
        latest_sql_schema = generated.group(1)

    assert latest_sql_schema is not None, "No catalog shape schema function found in SQL migrations"
    assert json.loads(latest_sql_schema) == _compact_shape_schema(expected)
