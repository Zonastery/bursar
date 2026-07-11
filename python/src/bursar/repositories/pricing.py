from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import ActivePricingRow

CallProc = Callable[[str, list[Any]], list[Any]]


class PricingRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def get_active_pricing(self) -> ActivePricingRow | None:
        rows = self._callproc("get_active_pricing_config", [])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def set_active_pricing(self, config: str, label: str | None) -> ActivePricingRow:
        rows = self._callproc("set_active_pricing_config", [config, label])
        return ActivePricingRow.model_validate(rows[0] if rows else {})

    def get_pricing_history(self) -> list[Any]:
        return self._callproc("get_pricing_configs", []) or []

    def get_pricing_config(self, version: int) -> ActivePricingRow | None:
        rows = self._callproc("get_pricing_config", [version])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def activate_pricing(self, version: int) -> ActivePricingRow:
        rows = self._callproc("activate_pricing_config", [version])
        return ActivePricingRow.model_validate(rows[0] if rows else {})
