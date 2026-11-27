"""Lossless JSON for persisted usage projections, without changing SDK inputs."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def json_dump(value: Any) -> str:
    """Encode arbitrary-precision decimal JSON numbers without quoting them."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number")
        number = format(value, "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return "0" if value.is_zero() else number
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(json.dumps(key, ensure_ascii=False) + ":" + json_dump(value[key]) for key in sorted(value))
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(json_dump(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def json_load(value: str) -> Any:
    """Decode JSON decimals without rounding at the storage boundary."""
    return json.loads(value, parse_float=Decimal)
