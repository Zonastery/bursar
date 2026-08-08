from __future__ import annotations

import json

from pydantic import ValidationError

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import optional_mapping_row, require_mapping_row
from bursar.credits.postgres.repositories.schemas import CatalogRevisionRow, CatalogRevisionSummaryRow
from bursar.errors import StoreError


class CatalogRepository:
    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    @staticmethod
    def _parse_revision(row: dict[str, object]) -> CatalogRevisionRow:
        parsed = {
            "id": str(row.get("id")),
            "config": row.get("source_document"),
            "version": row.get("revision_no"),
            "label": row.get("label"),
            "active": row.get("status") == "active",
            "status": row.get("status"),
            "created_at": row.get("created_at"),
        }
        try:
            return CatalogRevisionRow.model_validate(parsed)
        except ValidationError as error:
            raise StoreError("CatalogRepository returned an invalid catalog revision") from error

    def get_active_catalog(self) -> CatalogRevisionRow | None:
        rows = self._callproc("active_catalog_revision", []) or []
        row = optional_mapping_row(rows, "CatalogRepository.get_active_catalog")
        if row is None or all(value is None for value in row.values()):
            return None
        return self._parse_revision(row)

    def publish_and_activate_catalog(
        self,
        config: str,
        label: str | None,
        rollout: dict[str, object],
    ) -> CatalogRevisionRow:
        return self._publish_revision(config, label, rollout, activate=True)

    def publish_catalog_draft(self, config: str, label: str | None) -> CatalogRevisionRow:
        return self._publish_revision(config, label, {"plans": {}}, activate=False)

    def _publish_revision(
        self,
        config: str,
        label: str | None,
        rollout: dict[str, object],
        *,
        activate: bool,
    ) -> CatalogRevisionRow:
        row = require_mapping_row(
            self._callproc(
                "publish_and_activate_catalog",
                [1, json.loads(config), label, activate, rollout],
            ),
            "CatalogRepository.publish_revision",
        )
        revision_no = row.get("revision_no")
        if not isinstance(revision_no, int):
            raise StoreError("CatalogRepository.publish_revision returned an invalid revision_no")
        revision = self.get_catalog_revision(revision_no)
        if revision is None:
            raise StoreError(f"Catalog revision {revision_no} disappeared after publication")
        return revision

    def get_catalog_history(self) -> list[CatalogRevisionSummaryRow]:
        rows = self._callproc("list_catalog_revisions", [500]) or []
        revisions: list[CatalogRevisionSummaryRow] = []
        for row in rows:
            if not isinstance(row, dict):
                raise StoreError("CatalogRepository.get_catalog_history returned a non-object row")
            revision = self._parse_revision(row)
            revisions.append(
                CatalogRevisionSummaryRow(
                    id=revision.id,
                    version=revision.version,
                    label=revision.label,
                    active=revision.active,
                    created_at=revision.created_at,
                )
            )
        return revisions

    def get_catalog_revision(self, version: int) -> CatalogRevisionRow | None:
        rows = self._callproc("catalog_revision_by_number", [version]) or []
        row = optional_mapping_row(rows, "CatalogRepository.get_catalog_revision")
        if row is None or all(value is None for value in row.values()):
            return None
        return self._parse_revision(row)

    def activate_catalog_revision(
        self,
        version: int,
        rollout: dict[str, object],
    ) -> CatalogRevisionRow:
        rows = self._callproc("activate_catalog_revision", [version, rollout]) or []
        row = optional_mapping_row(rows, "CatalogRepository.activate_catalog_revision")
        if row is None or all(value is None for value in row.values()):
            raise StoreError(f"Catalog revision {version} was not found")
        return self._parse_revision(row)
