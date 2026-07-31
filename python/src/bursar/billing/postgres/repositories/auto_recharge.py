from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.credits.postgres.repositories.schemas import BillingTopupRow


class BillingTopupRepository:
    """Repository for credit topup resolution operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def resolve_by_price(
        self,
        provider: str,
        price_id: str | None,
        product_id: str | None,
    ) -> BillingTopupRow | None:
        """Resolve a credit topup by provider and price/product IDs.

        Args:
            provider: The billing provider identifier.
            price_id: The provider price ID, or None.
            product_id: The provider product ID, or None.

        Returns:
            BillingTopupRow if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        row = unwrap_jsonb(
            self._execute(
                "SELECT * FROM bursar.resolve_catalog_topup(%s, %s, %s)",
                [provider, "price_id" if price_id is not None else "product_id", price_id or product_id],
            )
        )
        return BillingTopupRow.model_validate(row) if row and row.get("topup_key") is not None else None

    def resolve_by_lookup(self, provider: str, lookup_key: str) -> BillingTopupRow | None:
        """Resolve a credit topup by provider and lookup key.

        Args:
            provider: The billing provider identifier.
            lookup_key: The topup lookup key.

        Returns:
            BillingTopupRow if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        row = unwrap_jsonb(
            self._execute(
                "SELECT * FROM bursar.resolve_catalog_topup(%s, 'external_id', %s)",
                [provider, lookup_key],
            )
        )
        return BillingTopupRow.model_validate(row) if row and row.get("topup_key") is not None else None
