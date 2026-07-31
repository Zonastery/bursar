"""Emit deterministic SDK contract results for the cross-language CI gate."""

from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from bursar.config import load_config_from_dict
from bursar.expr import ExpressionError, evaluate_expression

ROOT = Path(__file__).parents[2]


def main(output: str) -> None:
    expression_cases = json.loads((ROOT / "tests/parity/expression_cases.json").read_text())["expression_cases"]
    config_cases = json.loads((ROOT / "tests/parity/config_validation_cases.json").read_text())["cases"]
    expressions: dict[str, str] = {}
    for case in expression_cases:
        try:
            value = evaluate_expression(case["expr"], case.get("vars", {}))
            expressions[case["name"]] = f"{value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}"
        except ExpressionError:
            expressions[case["name"]] = "error"

    configs: dict[str, str] = {}
    for case in config_cases:
        try:
            load_config_from_dict(case["config"])
            configs[case["name"]] = "accept"
        except Exception:
            configs[case["name"]] = "reject"

    Path(output).write_text(json.dumps({"expressions": expressions, "configs": configs}, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
