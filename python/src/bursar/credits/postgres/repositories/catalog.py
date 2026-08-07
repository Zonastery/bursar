from __future__ import annotations

import json

from pydantic import ValidationError

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories.schemas import CatalogRevisionRow, CatalogRevisionSummaryRow
from bursar.errors import StoreError


class CatalogRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    @staticmethod
    def _parse_revision(row: dict[str, object]) -> CatalogRevisionRow:
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
        try:
            return CatalogRevisionRow.model_validate(parsed)
        except ValidationError as error:
            raise StoreError("CatalogRepository returned an invalid catalog revision") from error

    def get_active_catalog(self) -> CatalogRevisionRow | None:
        rows = self._callproc("active_catalog_revision", []) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        if not row or all(value is None for value in row.values()):
            return None
        return self._parse_revision(row)

    def publish_and_activate_catalog(
        self,
        config: str,
        label: str | None,
        rollout: dict[str, object],
    ) -> CatalogRevisionRow | None:
        return self._publish_revision(config, label, rollout, activate=True)

    def publish_catalog_draft(self, config: str, label: str | None) -> CatalogRevisionRow | None:
        return self._publish_revision(config, label, {"plans": {}}, activate=False)

    def _publish_revision(
        self,
        config: str,
        label: str | None,
        rollout: dict[str, object],
        *,
        activate: bool,
    ) -> CatalogRevisionRow | None:
        rows = (
            self._callproc(
                "publish_and_activate_catalog",
                [1, json.loads(config), label, activate, rollout],
            )
            or []
        )
        if not rows or not isinstance(rows[0], dict) or rows[0].get("revision_no") is None:
            return None
        return self.get_catalog_revision(int(rows[0]["revision_no"]))

    def get_catalog_history(self) -> list[CatalogRevisionSummaryRow]:
        rows = self._callproc("list_catalog_revisions", [500]) or []
        return [
            CatalogRevisionSummaryRow.model_validate(self._parse_revision(dict(row)).model_dump())
            for row in rows
            if isinstance(row, dict)
        ]

    def get_catalog_revision(self, version: int) -> CatalogRevisionRow | None:
        rows = self._callproc("catalog_revision_by_number", [version]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        if not row or all(value is None for value in row.values()):
            return None
        return self._parse_revision(row)

    def activate_catalog_revision(
        self,
        version: int,
        rollout: dict[str, object],
    ) -> CatalogRevisionRow | None:
        rows = self._callproc("activate_catalog_revision", [version, rollout]) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        row = dict(rows[0])
        if not row or all(value is None for value in row.values()):
            return None
        return self._parse_revision(row)
