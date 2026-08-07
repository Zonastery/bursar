from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from bursar.billing.contracts import (
    AutoRechargeAttemptClaim,
    AutoRechargeAttemptUpdate,
    AutoRechargeProviderPaymentUpdate,
)
from bursar.billing.types import BillingAutoRechargeAttempt, BillingAutoRechargeProfile
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_boolean_result,
    require_mapping_row,
)
from bursar.errors import StoreError
from bursar.shared.diagnostics import optional_bounded_diagnostic_message

_AttemptState = Literal[
    "claimed",
    "submitted",
    "processing",
    "unknown",
    "succeeded",
    "failed",
    "action_required",
]


class _RowModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ProfileRow(_RowModel):
    subject_id: UUID
    enabled: StrictBool
    armed: StrictBool
    state: Literal["disabled", "active", "paused"]
    provider: str | None
    topup_id: UUID | None
    quantity: int = Field(gt=0)
    threshold: Decimal = Field(ge=0)
    max_charges_per_window: int | None = Field(gt=0)
    window_unit: Literal["second", "minute", "hour", "day", "week", "month", "year"]
    window_count: int = Field(gt=0)
    window_anchor: Literal["calendar", "rolling"]
    window_timezone: str = Field(min_length=1)
    updated_at: datetime

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("provider must not be empty")
        return value

    @field_validator("updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("updated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_enabled_profile(self) -> Self:
        if self.enabled != (self.state != "disabled"):
            raise ValueError("enabled and state are inconsistent")
        if self.enabled and (self.provider is None or self.topup_id is None):
            raise ValueError("enabled profile requires provider and topup_id")
        return self


class _AttemptRow(_RowModel):
    id: UUID
    subject_id: UUID
    provider: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    provider_attempt_id: str | None = Field(min_length=1)
    topup_id: UUID
    quantity: int = Field(gt=0)
    state: _AttemptState
    window_start: datetime
    window_end: datetime
    quoted_amount_minor: int | None = Field(ge=0, le=9_007_199_254_740_991)
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    failure_code: str | None = Field(min_length=1)
    failure_message: str | None = Field(min_length=1)
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @field_validator("window_start", "window_end", "created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("auto-recharge timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_window_and_quote(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be later than window_start")
        if (self.quoted_amount_minor is None) != (self.currency is None):
            raise ValueError("quoted_amount_minor and currency must be present together")
        return self


def _validate_row(model: type[_ProfileRow] | type[_AttemptRow], row: dict[str, Any], context: str):
    try:
        return model.model_validate(row)
    except ValueError as error:
        raise StoreError(f"{context}: row validation failed", cause=error, details={"context": context}) from error


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _profile_from_row(row: dict[str, Any]) -> BillingAutoRechargeProfile:
    parsed = _validate_row(_ProfileRow, row, "BillingAutoRechargeRepository.profile")
    assert isinstance(parsed, _ProfileRow)
    return BillingAutoRechargeProfile(
        user_id=str(parsed.subject_id),
        enabled=parsed.enabled,
        armed=parsed.armed,
        state=parsed.state,
        provider=parsed.provider,
        topup_id=str(parsed.topup_id) if parsed.topup_id is not None else None,
        quantity=parsed.quantity,
        threshold=parsed.threshold,
        max_charges_per_window=parsed.max_charges_per_window,
        window_unit=parsed.window_unit,
        window_count=parsed.window_count,
        window_anchor=parsed.window_anchor,
        window_timezone=parsed.window_timezone,
        updated_at=_iso(parsed.updated_at),
    )


def _attempt_from_row(row: dict[str, Any]) -> BillingAutoRechargeAttempt:
    parsed = _validate_row(_AttemptRow, row, "BillingAutoRechargeRepository.attempt")
    assert isinstance(parsed, _AttemptRow)
    return BillingAutoRechargeAttempt(
        id=str(parsed.id),
        user_id=str(parsed.subject_id),
        provider=parsed.provider,
        idempotency_key=parsed.idempotency_key,
        provider_attempt_id=parsed.provider_attempt_id,
        topup_id=str(parsed.topup_id),
        quantity=parsed.quantity,
        state=parsed.state,
        window_start=_iso(parsed.window_start),
        window_end=_iso(parsed.window_end),
        quoted_amount_minor=parsed.quoted_amount_minor,
        currency=parsed.currency,
        failure_code=parsed.failure_code,
        failure_message=parsed.failure_message,
        metadata=parsed.metadata,
        created_at=_iso(parsed.created_at),
        updated_at=_iso(parsed.updated_at),
    )


_TRANSITIONS: dict[str, dict[str, list[str]]] = {
    "claimed": {
        "submitted": ["submitted"],
        "processing": ["submitted", "processing"],
        "succeeded": ["submitted", "processing", "succeeded"],
        "failed": ["submitted", "processing", "failed"],
        "unknown": ["submitted", "processing", "unknown"],
        "action_required": ["submitted", "action_required"],
    },
    "submitted": {
        "submitted": [],
        "processing": ["processing"],
        "succeeded": ["processing", "succeeded"],
        "failed": ["processing", "failed"],
        "unknown": ["processing", "unknown"],
        "action_required": ["action_required"],
    },
    "processing": {
        "processing": [],
        "succeeded": ["succeeded"],
        "failed": ["failed"],
        "unknown": ["unknown"],
        "action_required": ["action_required"],
    },
    "unknown": {
        "unknown": [],
        "processing": ["processing"],
        "succeeded": ["succeeded"],
        "failed": ["failed"],
        "action_required": ["action_required"],
    },
    "action_required": {
        "action_required": [],
        "processing": ["processing"],
        "succeeded": ["succeeded"],
        "failed": ["failed"],
    },
}


class BillingAutoRechargeRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def get_profile(self, user_id: str) -> BillingAutoRechargeProfile | None:
        rows = self._execute("SELECT * FROM bursar.get_auto_recharge_profile(%s::uuid)", [user_id])
        row = optional_mapping_row(rows, "BillingAutoRechargeRepository.get_profile")
        return None if row is None else _profile_from_row(row)

    def upsert_profile(self, profile: BillingAutoRechargeProfile, *, reset_cooldown: bool = False) -> None:
        rows = self._execute(
            """SELECT bursar.upsert_auto_recharge_profile(
                   %s::uuid,%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
               ) AS updated""",
            [
                profile.user_id,
                profile.enabled,
                profile.provider,
                profile.topup_id,
                profile.quantity,
                profile.threshold,
                profile.max_charges_per_window,
                profile.window_unit,
                profile.window_count,
                profile.window_anchor,
                profile.window_timezone,
                profile.armed,
                profile.state,
                reset_cooldown,
            ],
        )
        if not require_boolean_result(rows, "updated", "BillingAutoRechargeRepository.upsert_profile"):
            raise StoreError(f"auto-recharge profile update rejected: {profile.user_id}")

    def claim_attempt(self, input: AutoRechargeAttemptClaim) -> BillingAutoRechargeAttempt | None:
        rows = self._execute(
            "SELECT * FROM bursar.claim_auto_recharge_attempt(%s::uuid,%s)",
            [input.user_id, input.idempotency_key],
        )
        row = optional_mapping_row(rows, "BillingAutoRechargeRepository.claim_attempt")
        return None if row is None else _attempt_from_row(row)

    def _advance_attempt(self, input: AutoRechargeAttemptUpdate) -> None:
        current_rows = self._execute("SELECT * FROM bursar.get_auto_recharge_attempt(%s::uuid)", [input.id])
        current_row = optional_mapping_row(current_rows, "BillingAutoRechargeRepository.advance_attempt.current")
        if current_row is None:
            raise StoreError(f"auto-recharge attempt not found: {input.id}", details={"attempt_id": input.id})
        current = _attempt_from_row(current_row).state
        path = _TRANSITIONS.get(current, {}).get(input.state)
        if path is None:
            raise StoreError(
                f"auto-recharge attempt transition rejected: {input.id}",
                details={"attempt_id": input.id, "current_state": current, "requested_state": input.state},
            )
        failure_message = optional_bounded_diagnostic_message(input.failure_message)
        for next_state in path:
            rows = self._execute(
                """SELECT bursar.advance_auto_recharge_attempt(
                       %s::uuid,%s::bursar.recharge_attempt_status,%s,%s,%s,%s::jsonb
                   ) AS advanced""",
                [
                    input.id,
                    next_state,
                    input.provider_attempt_id,
                    input.failure_code,
                    failure_message,
                    json.dumps(input.metadata or {}),
                ],
            )
            if not require_boolean_result(rows, "advanced", "BillingAutoRechargeRepository.advance_attempt"):
                raise StoreError(
                    f"auto-recharge attempt transition rejected: {input.id}",
                    details={"attempt_id": input.id, "requested_state": next_state},
                )

    def update_attempt(self, input: AutoRechargeAttemptUpdate) -> None:
        self._advance_attempt(input)

    def update_attempt_by_provider_payment(self, input: AutoRechargeProviderPaymentUpdate) -> None:
        rows = self._execute(
            "SELECT * FROM bursar.get_auto_recharge_attempt_by_provider(%s,%s)",
            [input.provider, input.provider_payment_id],
        )
        row = optional_mapping_row(rows, "BillingAutoRechargeRepository.update_attempt_by_provider_payment")
        if row is None:
            return
        attempt = _attempt_from_row(row)
        self._advance_attempt(
            AutoRechargeAttemptUpdate(
                id=attempt.id,
                state=input.state,
                provider_attempt_id=input.provider_payment_id,
                failure_code=input.failure_code,
                failure_message=input.failure_message,
            )
        )

    def count_attempts(self, user_id: str, since: str | datetime) -> int:
        since_date = since if isinstance(since, datetime) else datetime.fromisoformat(since)
        if since_date.tzinfo is None:
            raise ValueError("auto-recharge attempt window must include timezone")
        rows = self._execute(
            "SELECT bursar.count_auto_recharge_attempts(%s::uuid,%s::timestamptz) AS count",
            [user_id, since_date.astimezone(UTC).isoformat()],
        )
        value = require_mapping_row(rows, "BillingAutoRechargeRepository.count_attempts").get("count")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StoreError("auto-recharge attempt count returned a malformed value")
        return value
