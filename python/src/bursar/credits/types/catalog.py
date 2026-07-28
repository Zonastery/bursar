"""Catalog types — mirrors JS SDK's ``credits/types/catalog.ts``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


class BursarConfigResult(BaseModel):
    id: str
    config: dict[str, Any] | None = None
    version: int = 1
    publication_version: int | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _sync_publication_version(self) -> BursarConfigResult:
        if self.publication_version is None:
            self.publication_version = self.version
        return self


class BursarConfigHistoryItem(BaseModel):
    id: str
    version: int
    label: str | None = None
    active: bool = False
    created_at: str = ""


class PricingConfig(BaseModel):
    """Pricing config wrapper for store results."""

    pass
