"""Cross-SDK config-validation parity test — Python side.

Loads ``tests/parity/config_validation_cases.json`` and asserts
``load_config_from_dict`` accepts/rejects each case exactly as documented.
The JS counterpart (``javascript/tests/config-parity.test.ts``) runs the same
fixture through ``loadConfigFromDict`` — this is the guard against the
Python<->JS validation drift found in the config schema review (missing
``version`` check, unvalidated ``signup_bonus``/``free_allowance`` sign,
silently-ignored unknown keys, dropped ``per_operation``, etc).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bursar.config import ConfigError, load_config_from_dict

_PARITY_PATH = Path(__file__).parent / "../../tests/parity/config_validation_cases.json"


def _load_cases() -> list[dict]:
    with _PARITY_PATH.open() as f:
        return json.load(f)["cases"]


_CASES = _load_cases()


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_config_validation_parity(case: dict) -> None:
    if case["expect"] == "accept":
        load_config_from_dict(case["config"])
    else:
        with pytest.raises((ConfigError, ValidationError)):
            load_config_from_dict(case["config"])
