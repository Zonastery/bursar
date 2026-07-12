from __future__ import annotations

from bursar.repositories._types import QueryFn
from bursar.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.repositories.schemas import BillingTopupRow


class BillingTopupRepository:
    """Repository for credit topup resolution operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

    def __init__(self, execute: QueryFn) -> None:
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
                "SELECT * FROM public.resolve_credit_topup_by_price(%s, %s, %s)",
                [provider, price_id, product_id],
            )
        )
        return BillingTopupRow.model_validate(row) if row else None

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
                "SELECT * FROM public.resolve_credit_topup_by_lookup(%s, %s)",
                [provider, lookup_key],
            )
        )
        return BillingTopupRow.model_validate(row) if row else None
