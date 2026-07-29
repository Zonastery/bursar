from __future__ import annotations

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.credits.postgres.repositories.schemas import BillingOfferRow


class BillingOfferRepository:
    """Repository for billing offer resolution operations.

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
    ) -> BillingOfferRow | None:
        """Resolve a billing offer by provider and price/product IDs.

        Args:
            provider: The billing provider identifier.
            price_id: The provider price ID, or None.
            product_id: The provider product ID, or None.

        Returns:
            BillingOfferRow if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        row = unwrap_jsonb(
            self._execute(
                "SELECT * FROM bursar.resolve_catalog_offer(%s, %s, %s)",
                [provider, "price_id" if price_id is not None else "product_id", price_id or product_id],
            )
        )
        if not row or row.get("offer_key") is None:
            return None
        plan = self._execute(
            "SELECT * FROM bursar.resolve_catalog_plan(%s, %s, %s)",
            [provider, "price_id" if price_id is not None else "product_id", price_id or product_id],
        )
        row.update(
            {
                "plan": row.get("plan_key"),
                "interval": row.get("billing_unit"),
                "interval_count": row.get("billing_count"),
                "grant_mode": "cycle_grant" if row.get("cycle_grant_amount") is not None else None,
                "grant_credits": row.get("cycle_grant_amount"),
                "grant_bucket": row.get("cycle_grant_bucket_key"),
                "grant_replace_prior": row.get("cycle_grant_renewal") == "replace_previous",
                "plan_id": plan[0].get("id") if plan and isinstance(plan[0], dict) else None,
            }
        )
        return BillingOfferRow.model_validate(row)

    def resolve_by_lookup(self, provider: str, lookup_key: str) -> BillingOfferRow | None:
        """Resolve a billing offer by provider and lookup key.

        Args:
            provider: The billing provider identifier.
            lookup_key: The offer lookup key.

        Returns:
            BillingOfferRow if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        row = unwrap_jsonb(
            self._execute(
                "SELECT * FROM bursar.resolve_catalog_offer(%s, 'external_id', %s)",
                [provider, lookup_key],
            )
        )
        if not row or row.get("offer_key") is None:
            return None
        plan = self._execute(
            "SELECT * FROM bursar.resolve_catalog_plan(%s, 'external_id', %s)",
            [provider, lookup_key],
        )
        row.update(
            {
                "plan": row.get("plan_key"),
                "interval": row.get("billing_unit"),
                "interval_count": row.get("billing_count"),
                "grant_mode": "cycle_grant" if row.get("cycle_grant_amount") is not None else None,
                "grant_credits": row.get("cycle_grant_amount"),
                "grant_bucket": row.get("cycle_grant_bucket_key"),
                "grant_replace_prior": row.get("cycle_grant_renewal") == "replace_previous",
                "plan_id": plan[0].get("id") if plan and isinstance(plan[0], dict) else None,
            }
        )
        return BillingOfferRow.model_validate(row)
