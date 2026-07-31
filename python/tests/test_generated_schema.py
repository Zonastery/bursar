"""The checked-in JSON Schema must match the Pydantic source model."""

from __future__ import annotations

import json
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
    ]
    for output in outputs:
        assert json.loads(output.read_text(encoding="utf-8")) == expected

    sql = (repository / "python" / "src" / "bursar" / "sql" / "001_schema_and_types.sql").read_text(encoding="utf-8")
    generated = sql.split("$catalog_json$", 2)[1]
    assert json.loads(generated) == _compact_shape_schema(expected)
