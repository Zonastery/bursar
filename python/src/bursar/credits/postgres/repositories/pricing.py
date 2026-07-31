from __future__ import annotations

import json

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories.schemas import ActivePricingRow, BursarConfigHistoryItemRow


class PricingRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    @staticmethod
    def _parse_revision(row: dict[str, object]) -> ActivePricingRow:
        parsed = dict(row)
        if parsed.get("id") is not None:
            parsed["id"] = str(parsed["id"])
        parsed.update(
            {
                "config": parsed.get("source_document"),
                "version": parsed.get("revision_no"),
                "active": parsed.get("status") == "active",
            }
        )
        return ActivePricingRow.model_validate(parsed)

    def get_active_pricing(self) -> ActivePricingRow | None:
        rows = self._callproc("active_catalog_revision", []) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        if not row or all(value is None for value in row.values()):
            return None
        return self._parse_revision(row)

    def set_active_pricing(self, config: str, label: str | None) -> ActivePricingRow | None:
        return self._publish_pricing(config, label, activate=True)

    def publish_pricing(self, config: str, label: str | None) -> ActivePricingRow | None:
        return self._publish_pricing(config, label, activate=False)

    def _publish_pricing(self, config: str, label: str | None, *, activate: bool) -> ActivePricingRow | None:
        rows = self._callproc("publish_and_activate_catalog", [1, json.loads(config), label, activate]) or []
        if not rows or not isinstance(rows[0], dict) or rows[0].get("revision_no") is None:
            return None
        return self.get_bursar_config(int(rows[0]["revision_no"]))

    def get_pricing_history(self) -> list[BursarConfigHistoryItemRow]:
        rows = self._callproc("list_catalog_revisions", [500]) or []
        return [
            BursarConfigHistoryItemRow.model_validate(self._parse_revision(dict(row)).model_dump())
            for row in rows
            if isinstance(row, dict)
        ]

    def get_bursar_config(self, version: int) -> ActivePricingRow | None:
        rows = self._callproc("catalog_revision_by_number", [version]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        return self._parse_revision(dict(rows[0]))

    def activate_pricing(self, version: int) -> ActivePricingRow | None:
        rows = self._callproc("activate_catalog_revision", [version]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        return self._parse_revision(dict(rows[0]))
