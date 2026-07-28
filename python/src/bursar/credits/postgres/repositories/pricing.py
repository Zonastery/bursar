from __future__ import annotations

import json

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories.schemas import ActivePricingRow, BursarConfigHistoryItemRow


class PricingRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def get_active_pricing(self) -> ActivePricingRow | None:
        rows = self._callproc("active_catalog_revision", []) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        if not row or all(value is None for value in row.values()):
            return None
        if row.get("id") is not None:
            row["id"] = str(row["id"])
        row.update(
            {
                "config": row.get("source_document"),
                "version": row.get("revision_no"),
                "active": row.get("status") == "active",
            }
        )
        return ActivePricingRow.model_validate(row)

    def set_active_pricing(self, config: str, label: str | None) -> ActivePricingRow | None:
        return self.publish_pricing(config, label)

    def publish_pricing(self, config: str, label: str | None) -> ActivePricingRow | None:
        self._callproc("publish_and_activate_catalog", [1, json.loads(config), label])
        return self.get_active_pricing()

    def get_pricing_history(self) -> list[BursarConfigHistoryItemRow]:
        row = self.get_active_pricing()
        return [BursarConfigHistoryItemRow.model_validate(row.model_dump())] if row else []

    def get_bursar_config(self, version: int) -> ActivePricingRow | None:
        row = self.get_active_pricing()
        return row if row and row.version == version else None

    def activate_pricing(self, version: int) -> ActivePricingRow | None:
        return self.get_bursar_config(version)
