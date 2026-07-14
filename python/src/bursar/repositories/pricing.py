from __future__ import annotations

from bursar.repositories._types import DbQuery
from bursar.repositories.schemas import ActivePricingRow, PricingConfigHistoryItemRow


class PricingRepository:
    """Repository for pricing configuration operations.

    All methods call Postgres RPCs via the callproc function.
    Returns None when the RPC returns no rows (no result).
    Returns typed Pydantic models for successful results.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def get_active_pricing(self) -> ActivePricingRow | None:
        """Get the currently active pricing configuration.

        Returns:
            ActivePricingRow if found, None otherwise.
        """
        rows = self._callproc("get_active_pricing_config", [])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def set_active_pricing(self, config: str, label: str | None) -> ActivePricingRow | None:
        """Set a new active pricing configuration.

        Args:
            config: JSON string containing the pricing configuration.
            label: Optional human-readable label for this version.

        Returns:
            ActivePricingRow for the newly activated config, or None on failure.
        """
        rows = self._callproc("set_active_pricing_config", [config, label])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0])

    def get_pricing_history(self) -> list[PricingConfigHistoryItemRow]:
        """Get all pricing configuration versions.

        Returns:
            List of PricingConfigHistoryItemRow (may be empty).
        """
        rows = self._callproc("get_pricing_configs", []) or []
        return [PricingConfigHistoryItemRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def get_pricing_config(self, version: int) -> ActivePricingRow | None:
        """Get a specific pricing configuration by version number.

        Args:
            version: The version number to retrieve.

        Returns:
            ActivePricingRow if found, None otherwise.
        """
        rows = self._callproc("get_pricing_config", [version])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def activate_pricing(self, version: int) -> ActivePricingRow | None:
        """Activate a specific pricing configuration version."""
        rows = self._callproc("activate_pricing_config", [version])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0])

    def publish_pricing(self, config: str, label: str | None) -> ActivePricingRow | None:
        """Publish an inactive pricing configuration draft."""
        rows = self._callproc("publish_pricing_config", [config, label])
        if not rows:
            return None
        return ActivePricingRow.model_validate(rows[0])
