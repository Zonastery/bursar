#!/usr/bin/env python3
"""Generate docs/pricing-config.schema.json from PricingConfig.model_json_schema()."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running without installing the package (src layout).
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from bursar.config import PricingConfig  # noqa: E402

_REPO_ROOT = _ROOT.parent
OUTPUT = _REPO_ROOT / "docs" / "pricing-config.schema.json"


def main() -> None:
    schema = PricingConfig.model_json_schema()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
