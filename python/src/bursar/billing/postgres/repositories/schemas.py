"""Validated database row contracts for the PostgreSQL billing adapter."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class _BillingRow(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BillingOfferRow(_BillingRow):
    id: UUID
    plan_id: UUID
    offer_key: str = Field(min_length=1)
    plan: str = Field(min_length=1)
    interval: Literal["day", "week", "month", "year"]
    interval_count: int = Field(gt=0)
    grant_mode: Literal["cycle_grant"] | None
    grant_credits: Decimal | None = Field(gt=0)
    grant_bucket: str | None = Field(min_length=1)
    grant_replace_prior: StrictBool

    @model_validator(mode="after")
    def validate_cycle_grant(self) -> Self:
        grant_fields = (self.grant_mode, self.grant_credits, self.grant_bucket)
        if any(value is None for value in grant_fields) != all(value is None for value in grant_fields):
            raise ValueError("cycle grant fields must either all be set or all be null")
        if self.grant_mode is None and self.grant_replace_prior:
            raise ValueError("grant_replace_prior requires a cycle grant")
        return self


class BillingTopupRow(_BillingRow):
    id: UUID
    topup_key: str = Field(min_length=1)
    credits_per_unit: Decimal = Field(gt=0)
    bucket_key: str = Field(min_length=1)
    amount_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    min_quantity: int = Field(gt=0)
    max_quantity: int = Field(gt=0)
    default_quantity: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_quantity_bounds(self) -> Self:
        if self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity is below min_quantity")
        if not self.min_quantity <= self.default_quantity <= self.max_quantity:
            raise ValueError("default_quantity is outside the configured range")
        return self


class CatalogOfferContextRow(_BillingRow):
    offer_key: str = Field(min_length=1)
    plan_id: UUID
    plan_key: str = Field(min_length=1)
    billing_unit: Literal["day", "week", "month", "year"]
    billing_count: int = Field(gt=0)


class PersistedSubscriptionRow(_BillingRow):
    id: UUID
    subject_id: UUID
    provider: str = Field(min_length=1)
    provider_subscription_id: str = Field(min_length=1)
    provider_customer_id: str | None = Field(min_length=1)
    offer_id: UUID
    catalog_revision_id: UUID
    status: Literal[
        "incomplete",
        "incomplete_expired",
        "trialing",
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
        "expired",
    ]
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at: datetime | None
    ended_at: datetime | None
    cancel_at_period_end: StrictBool
    grace_ends_at: datetime | None
    grace_expired_at: datetime | None
    provider_updated_at: datetime
    metadata: dict[str, Any]

    @field_validator(
        "current_period_start",
        "current_period_end",
        "trial_end",
        "cancel_at",
        "ended_at",
        "grace_ends_at",
        "grace_expired_at",
        "provider_updated_at",
    )
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("subscription timestamps must include a timezone")
        return value


class SubscriptionRow(_BillingRow):
    id: UUID
    user_id: UUID
    provider: str = Field(min_length=1)
    provider_subscription_id: str = Field(min_length=1)
    provider_customer_id: str | None
    offer_id: UUID
    offer_key: str = Field(min_length=1)
    plan_id: UUID
    plan: str = Field(min_length=1)
    status: Literal[
        "incomplete",
        "incomplete_expired",
        "trialing",
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
        "expired",
    ]
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at: datetime | None
    ended_at: datetime | None
    cancel_at_period_end: StrictBool
    interval: Literal["day", "week", "month", "year"]
    interval_count: int = Field(gt=0)
    grace_ends_at: datetime | None
    grace_expired_at: datetime | None
    provider_updated_at: datetime
    metadata: dict[str, Any]

    @field_validator(
        "current_period_start",
        "current_period_end",
        "trial_end",
        "cancel_at",
        "ended_at",
        "grace_ends_at",
        "grace_expired_at",
        "provider_updated_at",
    )
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("subscription timestamps must include a timezone")
        return value


class BillingEventRow(_BillingRow):
    event_id: UUID | None
    status: Literal[
        "claimed",
        "duplicate",
        "busy",
        "invalid_request",
        "idempotency_conflict",
        "max_retries_exceeded",
    ]
    claim_token: UUID | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "claimed":
            if self.event_id is None or self.claim_token is None:
                raise ValueError("claimed billing event requires event_id and claim_token")
        elif self.claim_token is not None:
            raise ValueError("unclaimed billing event cannot expose a claim_token")
        if self.status == "invalid_request" and self.event_id is not None:
            raise ValueError("invalid billing event request cannot identify a stored event")
        if self.status not in {"claimed", "invalid_request"} and self.event_id is None:
            raise ValueError("stored billing event outcome requires event_id")
        return self


class BillingPaymentRow(_BillingRow):
    id: UUID
    provider: str = Field(min_length=1)
    provider_payment_id: str = Field(min_length=1)
    provider_invoice_id: str | None
    subject_id: UUID
    amount_minor: int = Field(ge=0)
    tax_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    purpose: Literal["subscription", "credit_topup"]
    status: Literal["pending", "succeeded", "failed", "canceled"]
    provider_updated_at: datetime
    metadata: dict[str, Any]

    @field_validator("provider_updated_at")
    @classmethod
    def validate_provider_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("payment provider timestamp must include a timezone")
        return value
