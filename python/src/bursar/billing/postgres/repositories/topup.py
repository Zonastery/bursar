from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from bursar.billing.postgres.repositories.schemas import BillingTopupRow
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.errors import StoreError

_TOPUP_FIELDS = (
    "id",
    "topup_key",
    "credits_per_unit",
    "bucket_key",
    "amount_minor",
    "currency",
    "min_quantity",
    "max_quantity",
    "default_quantity",
)


class BillingTopupRepository:
    """Resolve provider references to active catalog top-ups."""

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def resolve_by_price(
        self,
        provider: str,
        price_id: str | None,
        product_id: str | None,
    ) -> BillingTopupRow | None:
        lookup_type = "price_id" if price_id is not None else "product_id"
        return self._resolve(provider, lookup_type, price_id or product_id, "resolve_by_price")

    def resolve_by_lookup(self, provider: str, lookup_key: str) -> BillingTopupRow | None:
        return self._resolve(provider, "external_id", lookup_key, "resolve_by_lookup")

    def _resolve(
        self,
        provider: str,
        lookup_type: Literal["price_id", "product_id", "external_id"],
        lookup_value: str | None,
        operation: str,
    ) -> BillingTopupRow | None:
        validate_non_empty(provider, "provider")
        if lookup_value is None or not lookup_value.strip():
            return None

        context = f"BillingTopupRepository.{operation}"
        rows = self._execute(
            "SELECT * FROM bursar.resolve_catalog_topup(%s, %s, %s)",
            [provider, lookup_type, lookup_value],
        )
        if len(rows) > 1:
            raise StoreError(
                f"{context}: expected at most one top-up",
                details={"row_count": len(rows)},
            )
        raw_topup = unwrap_jsonb(rows)
        if raw_topup is None:
            return None
        try:
            return BillingTopupRow.model_validate({field: raw_topup.get(field) for field in _TOPUP_FIELDS})
        except ValidationError as exc:
            raise StoreError(
                f"{context}: result validation failed",
                cause=exc,
                details={"context": context},
            ) from exc
