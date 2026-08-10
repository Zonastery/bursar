"""Protect the mirrored Python and JavaScript application contract."""

from __future__ import annotations

import json
from pathlib import Path

from bursar.bursar import AccountService, Bursar, CatalogService, CreditsCapability
from bursar.storage.runtime import BursarRuntime

CONTRACT = json.loads((Path(__file__).parents[2] / "common" / "facade-contract.json").read_text())

SURFACES = {
    "bursar": Bursar,
    "catalog": CatalogService,
    "accounts": AccountService,
    "credits": CreditsCapability,
    "runtime": BursarRuntime,
}


def test_python_exposes_every_mirrored_facade_operation() -> None:
    for surface, entries in CONTRACT.items():
        sdk_type = SURFACES[surface]
        for entry in entries:
            assert hasattr(sdk_type, entry["python"]), (
                f"Python {surface} is missing mirrored operation {entry['python']}"
            )
