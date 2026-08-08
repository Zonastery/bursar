from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bursar.billing.postgres.repositories.schemas import BillingOfferRow
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import unwrap_jsonb, validate_non_empty
from bursar.errors import StoreError


class _CatalogOfferRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    catalog_revision_id: UUID
    offer_key: str = Field(min_length=1)
    plan_key: str = Field(min_length=1)
    billing_unit: Literal["day", "week", "month", "year"]
    billing_count: int = Field(gt=0)
    cycle_grant_amount: Decimal | None = Field(gt=0)
    cycle_grant_bucket_key: str | None = Field(min_length=1)
    cycle_grant_renewal: Literal["replace_previous", "accumulate"] | None

    @model_validator(mode="after")
    def validate_cycle_grant(self) -> Self:
        fields = (
            self.cycle_grant_amount,
            self.cycle_grant_bucket_key,
            self.cycle_grant_renewal,
        )
        if any(value is None for value in fields) != all(value is None for value in fields):
            raise ValueError("cycle grant fields must either all be set or all be null")
        return self


class _OfferContextRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_key: str = Field(min_length=1)
    plan_id: UUID
    plan_key: str = Field(min_length=1)
    billing_unit: Literal["day", "week", "month", "year"]
    billing_count: int = Field(gt=0)


def _validate_row[T: BaseModel](model: type[T], value: object, context: str) -> T:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise StoreError(
            f"{context}: result validation failed",
            cause=exc,
            details={"context": context},
        ) from exc


class BillingOfferRepository:
    """Resolve provider references to revision-pinned catalog offers."""

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def resolve_by_price(
        self,
        provider: str,
        price_id: str | None,
        product_id: str | None,
    ) -> BillingOfferRow | None:
        lookup_type = "price_id" if price_id is not None else "product_id"
        return self._resolve(provider, lookup_type, price_id or product_id, "resolve_by_price")

    def resolve_by_lookup(self, provider: str, lookup_key: str) -> BillingOfferRow | None:
        return self._resolve(provider, "external_id", lookup_key, "resolve_by_lookup")

    def _resolve(
        self,
        provider: str,
        lookup_type: Literal["price_id", "product_id", "external_id"],
        lookup_value: str | None,
        operation: str,
    ) -> BillingOfferRow | None:
        validate_non_empty(provider, "provider")
        if lookup_value is None or not lookup_value.strip():
            return None

        context = f"BillingOfferRepository.{operation}"
        rows = self._execute(
            "SELECT * FROM bursar.resolve_catalog_offer(%s, %s, %s)",
            [provider, lookup_type, lookup_value],
        )
        if len(rows) > 1:
            raise StoreError(
                f"{context}: expected at most one offer",
                details={"row_count": len(rows)},
            )
        raw_offer = unwrap_jsonb(rows)
        if raw_offer is None:
            return None
        offer = _validate_row(
            _CatalogOfferRow,
            {
                "id": raw_offer.get("id"),
                "catalog_revision_id": raw_offer.get("catalog_revision_id"),
                "offer_key": raw_offer.get("offer_key"),
                "plan_key": raw_offer.get("plan_key"),
                "billing_unit": raw_offer.get("billing_unit"),
                "billing_count": raw_offer.get("billing_count"),
                "cycle_grant_amount": raw_offer.get("cycle_grant_amount"),
                "cycle_grant_bucket_key": raw_offer.get("cycle_grant_bucket_key"),
                "cycle_grant_renewal": raw_offer.get("cycle_grant_renewal"),
            },
            context,
        )

        context_rows = self._execute(
            "SELECT * FROM bursar.get_catalog_offer_context(%s::uuid, %s::uuid)",
            [offer.id, offer.catalog_revision_id],
        )
        if len(context_rows) != 1:
            raise StoreError(
                f"{context}: catalog plan context is missing",
                details={
                    "offer_id": str(offer.id),
                    "catalog_revision_id": str(offer.catalog_revision_id),
                },
            )
        offer_context = _validate_row(_OfferContextRow, context_rows[0], f"{context}.context")
        if (
            offer_context.offer_key != offer.offer_key
            or offer_context.plan_key != offer.plan_key
            or offer_context.billing_unit != offer.billing_unit
            or offer_context.billing_count != offer.billing_count
        ):
            raise StoreError(
                f"{context}: catalog plan context does not match the resolved offer",
                details={
                    "offer_id": str(offer.id),
                    "catalog_revision_id": str(offer.catalog_revision_id),
                },
            )

        return _validate_row(
            BillingOfferRow,
            {
                "id": offer.id,
                "plan_id": offer_context.plan_id,
                "offer_key": offer.offer_key,
                "plan": offer.plan_key,
                "interval": offer.billing_unit,
                "interval_count": offer.billing_count,
                "grant_mode": "cycle_grant" if offer.cycle_grant_amount is not None else None,
                "grant_credits": offer.cycle_grant_amount,
                "grant_bucket": offer.cycle_grant_bucket_key,
                "grant_replace_prior": offer.cycle_grant_renewal == "replace_previous",
            },
            context,
        )
