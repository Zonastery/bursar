"""The checked-in JSON Schema must match the Pydantic source model."""

from __future__ import annotations

import json
from pathlib import Path

from bursar.config import BursarConfig


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
