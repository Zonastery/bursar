"""Emit deterministic SDK contract results for the cross-language CI gate."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from bursar.allowance import resolve_allowance_window, resolve_calendar_window
from bursar.config import load_config_from_dict
from bursar.expr import ExpressionError, evaluate_expression

ROOT = Path(__file__).parents[2]


def main(output: str) -> None:
    expression_cases = json.loads((ROOT / "tests/parity/expression_cases.json").read_text())["expression_cases"]
    config_cases = json.loads((ROOT / "tests/parity/config_validation_cases.json").read_text())["cases"]
    allowance_cases = json.loads((ROOT / "tests/parity/allowance_cases.json").read_text())
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

    windows: dict[str, dict[str, str]] = {}
    for case in allowance_cases:
        now = datetime.fromisoformat(f"{case['now']}T12:00:00+00:00").astimezone(UTC)
        if case.get("feature"):
            start, end = resolve_calendar_window(now, case["period"])
        else:
            anchor = datetime.fromisoformat(f"{case['anchor']}T12:00:00+00:00") if case.get("anchor") else None
            start, end = resolve_allowance_window(now, case["period"], anchor)
        windows[case["name"]] = {"start": start.isoformat(), "end": end.isoformat()}

    Path(output).write_text(
        json.dumps({"expressions": expressions, "configs": configs, "windows": windows}, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main(sys.argv[1])
