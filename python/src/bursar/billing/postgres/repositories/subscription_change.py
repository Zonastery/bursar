"""Strict persistence boundary for durable subscription offer transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bursar.billing.contracts import BillingSubscriptionChangeUpdate
from bursar.billing.types import (
    BillingOfferInterval,
    BillingSubscriptionChange,
    BillingSubscriptionChangeInput,
    BillingSubscriptionOfferContext,
)
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_boolean_result,
    require_mapping_row,
)
from bursar.errors import StoreError
from bursar.shared.diagnostics import optional_bounded_diagnostic_message

_ChangeState = Literal["awaiting_payment", "scheduled", "applied", "failed", "canceled"]
_EffectiveBehavior = Literal["immediate", "renewal"]
_ProrationBehavior = Literal["provider_default", "invoice_immediately", "none"]
_OpenError = Literal[
    "invalid_request",
    "missing_subscription",
    "invalid_target_offer",
    "idempotency_conflict",
    "open_change_exists",
]


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _OpenChangeResult(_Row):
    change_id: int | None = Field(default=None, gt=0)
    state: _ChangeState | None = None
    error_code: _OpenError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        succeeded = self.error_code is None
        if succeeded != (self.change_id is not None and self.state is not None):
            raise ValueError("subscription-change result has inconsistent success fields")
        return self


class _ChangeRow(_Row):
    id: int = Field(gt=0)
    subscription_id: UUID
    from_offer_id: UUID
    from_catalog_revision_id: UUID
    to_offer_id: UUID
    to_catalog_revision_id: UUID
    effective_at: datetime | None
    effective_behavior: _EffectiveBehavior
    state: _ChangeState
    proration_behavior: _ProrationBehavior
    idempotency_key: str = Field(min_length=1)
    provider_operation_id: str | None = Field(min_length=1)
    error_message: str | None = Field(min_length=1, max_length=8192)

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("effective_at must include a timezone")
        return value


class _OfferContextRow(_Row):
    side: Literal["from", "to"]
    offer_id: UUID
    offer_key: str = Field(min_length=1)
    plan_id: UUID
    plan_key: str = Field(min_length=1)
    billing_unit: Literal["day", "week", "month", "year"]
    billing_count: int = Field(gt=0)


def _validate[T: BaseModel](model: type[T], value: object, context: str, *, indeterminate: bool = False) -> T:
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise StoreError(
            f"{context}: row validation failed",
            cause=error,
            indeterminate=indeterminate,
            details={"context": context, "model": model.__name__},
        ) from error


def _project_change(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "subscription_id": row.get("subscription_id"),
        "from_offer_id": row.get("from_offer_id"),
        "from_catalog_revision_id": row.get("from_catalog_revision_id"),
        "to_offer_id": row.get("to_offer_id"),
        "to_catalog_revision_id": row.get("to_catalog_revision_id"),
        "effective_at": row.get("effective_at"),
        "effective_behavior": row.get("effective_behavior"),
        "state": row.get("state"),
        "proration_behavior": row.get("proration_behavior"),
        "idempotency_key": row.get("idempotency_key"),
        "provider_operation_id": row.get("provider_operation_id"),
        "error_message": row.get("error_message"),
    }


def _project_context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": row.get("side"),
        "offer_id": row.get("offer_id"),
        "offer_key": row.get("offer_key"),
        "plan_id": row.get("plan_id"),
        "plan_key": row.get("plan_key"),
        "billing_unit": row.get("billing_unit"),
        "billing_count": row.get("billing_count"),
    }


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _public_context(row: _OfferContextRow) -> BillingSubscriptionOfferContext:
    return BillingSubscriptionOfferContext(
        offer_id=str(row.offer_id),
        offer_key=row.offer_key,
        plan_id=str(row.plan_id),
        plan=row.plan_key,
        interval=BillingOfferInterval(row.billing_unit),
        interval_count=row.billing_count,
    )


class BillingSubscriptionChangeRepository:
    """Persist and validate provider-neutral subscription offer changes."""

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def create(
        self,
        subscription_id: str,
        input: BillingSubscriptionChangeInput,
    ) -> BillingSubscriptionChange:
        rows = self._execute(
            """SELECT * FROM bursar.open_subscription_change(
                   %s::uuid,%s::uuid,%s::timestamptz,%s,%s,%s
               )""",
            [
                subscription_id,
                input.to_offer_id,
                input.effective_at,
                input.effective,
                input.idempotency_key,
                input.proration_behavior,
            ],
        )
        raw = require_mapping_row(rows, "BillingSubscriptionChangeRepository.create")
        result = _validate(
            _OpenChangeResult,
            {
                "change_id": raw.get("change_id"),
                "state": raw.get("state"),
                "error_code": raw.get("error_code"),
            },
            "BillingSubscriptionChangeRepository.create",
            indeterminate=True,
        )
        if result.error_code is not None:
            raise StoreError(
                f"subscription change rejected: {result.error_code}",
                details={"error_code": result.error_code},
            )
        if result.change_id is None:
            raise StoreError("subscription change returned no identifier", indeterminate=True)
        change = self.get_by_id(str(result.change_id))
        if change is None:
            raise StoreError(
                "subscription change could not be read after creation",
                indeterminate=True,
                details={"subscription_change_id": str(result.change_id)},
            )
        return change

    def get_by_id(self, id: str) -> BillingSubscriptionChange | None:
        rows = self._execute("SELECT * FROM bursar.get_billing_subscription_change(%s::bigint)", [id])
        return self._parse_optional(rows, "BillingSubscriptionChangeRepository.get_by_id")

    def get_open(self, provider: str, provider_subscription_id: str) -> BillingSubscriptionChange | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_open_billing_subscription_change(%s,%s)",
            [provider, provider_subscription_id],
        )
        return self._parse_optional(rows, "BillingSubscriptionChangeRepository.get_open")

    def update(self, id: str, update: BillingSubscriptionChangeUpdate) -> None:
        if update.state is None:
            return
        rows = self._execute(
            "SELECT bursar.advance_subscription_change(%s::bigint,%s,%s,%s) AS advanced",
            [
                id,
                update.state,
                update.provider_operation_id,
                optional_bounded_diagnostic_message(update.error_message),
            ],
        )
        if not require_boolean_result(rows, "advanced", "BillingSubscriptionChangeRepository.update"):
            raise StoreError(
                f"subscription change transition rejected: {id}",
                details={"subscription_change_id": id},
            )

    def _parse_optional(self, rows: list[Any], context: str) -> BillingSubscriptionChange | None:
        raw = optional_mapping_row(rows, context)
        if raw is None:
            return None
        row = _validate(_ChangeRow, _project_change(raw), context)
        from_offer, to_offer = self._offer_contexts(row, context)
        return BillingSubscriptionChange(
            id=str(row.id),
            subscription_id=str(row.subscription_id),
            from_offer_id=str(row.from_offer_id),
            to_offer_id=str(row.to_offer_id),
            from_offer=_public_context(from_offer),
            to_offer=_public_context(to_offer),
            effective_at=_iso(row.effective_at),
            effective=row.effective_behavior,
            state=row.state,
            proration_behavior=row.proration_behavior,
            idempotency_key=row.idempotency_key,
            provider_operation_id=row.provider_operation_id,
            error_message=row.error_message,
        )

    def _offer_contexts(
        self,
        row: _ChangeRow,
        context: str,
    ) -> tuple[_OfferContextRow, _OfferContextRow]:
        rows = self._execute(
            """SELECT requested.side, requested.offer_id, offer_context.*
               FROM (
                   VALUES
                       ('from', %s::uuid, %s::uuid),
                       ('to', %s::uuid, %s::uuid)
               ) AS requested(side, offer_id, catalog_revision_id)
               CROSS JOIN LATERAL bursar.get_catalog_offer_context(
                   requested.offer_id,
                   requested.catalog_revision_id
               ) AS offer_context""",
            [
                row.from_offer_id,
                row.from_catalog_revision_id,
                row.to_offer_id,
                row.to_catalog_revision_id,
            ],
        )
        if len(rows) != 2 or any(not isinstance(candidate, dict) for candidate in rows):
            raise StoreError(
                f"{context}: expected both subscription-change offer contexts",
                details={"subscription_change_id": str(row.id), "row_count": len(rows)},
            )
        parsed = [
            _validate(_OfferContextRow, _project_context(candidate), f"{context}.context")
            for candidate in rows
            if isinstance(candidate, dict)
        ]
        by_side = {candidate.side: candidate for candidate in parsed}
        if len(by_side) != 2 or "from" not in by_side or "to" not in by_side:
            raise StoreError(
                f"{context}: duplicate or missing subscription-change offer context",
                details={"subscription_change_id": str(row.id)},
            )
        from_offer = by_side["from"]
        to_offer = by_side["to"]
        if from_offer.offer_id != row.from_offer_id or to_offer.offer_id != row.to_offer_id:
            raise StoreError(
                f"{context}: subscription-change offer context does not match",
                details={"subscription_change_id": str(row.id)},
            )
        return from_offer, to_offer
