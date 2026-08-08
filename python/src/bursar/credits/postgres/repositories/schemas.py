"""Pydantic models for raw database row shapes returned by repositories."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class BalanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)
    balance: str | Decimal
    lifetime_purchased: str | Decimal


class AddCreditsRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str | None
    user_id: str = Field(min_length=1)
    amount: str | Decimal
    new_balance: str | Decimal | None
    lifetime_purchased: str | Decimal | None
    bucket: str | None
    idempotent: bool
    error: str | None

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        amount = Decimal(str(self.amount))
        if self.error is None and (
            self.entry_id is None or self.new_balance is None or self.lifetime_purchased is None
        ):
            raise ValueError("successful credit postings require entry and balance fields")
        if self.error is None and amount > 0 and (self.bucket is None or not self.bucket.strip()):
            raise ValueError("successful positive credit postings require a destination bucket")
        if self.error is None and amount <= 0 and self.bucket is not None:
            raise ValueError("debit postings cannot claim a single destination bucket")
        if self.error is not None and (
            self.entry_id is not None
            or self.new_balance is not None
            or self.lifetime_purchased is not None
            or self.bucket is not None
            or self.idempotent
        ):
            raise ValueError("failed credit postings cannot expose committed result fields")
        return self


class AvailableRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    balance: str | Decimal
    reserved: str | Decimal
    available: str | Decimal


class GrantProgramAwardRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_event_id: UUID | None
    grant_award_id: UUID | None
    recipient_subject_id: UUID | None
    ledger_entry_id: UUID | None
    amount: str | Decimal | None
    replayed: StrictBool
    error_code: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        award = (
            self.grant_event_id,
            self.grant_award_id,
            self.recipient_subject_id,
            self.ledger_entry_id,
            self.amount,
        )
        if self.error_code is None and any(value is None for value in award):
            raise ValueError("successful grant awards require committed fields")
        if self.error_code is not None and (any(value is not None for value in award) or self.replayed):
            raise ValueError("failed grant awards cannot expose committed fields")
        return self


class DeductionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: UUID | None
    # Allowance-only and zero-cost usage has no monetary ledger entry.
    entry_id: UUID | None
    amount: str | Decimal
    balance_after: str | Decimal | None
    allowance_consumed: str | Decimal
    idempotent: StrictBool
    bucket_breakdown: dict[str, str | Decimal] | None
    error: str | None
    user_id: UUID

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if self.error is None and (self.charge_id is None or self.balance_after is None):
            raise ValueError("successful usage charges require a receipt and committed balance")
        if self.error is not None and (
            self.charge_id is not None or self.entry_id is not None or self.balance_after is not None or self.idempotent
        ):
            raise ValueError("failed usage charges cannot expose committed fields")
        return self


class ChargeRpcRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: UUID | None
    ledger_entry_id: UUID | None
    charged: str | Decimal
    allowance_covered: str | Decimal
    replayed: StrictBool
    error_code: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.error_code is None and self.charge_id is None:
            raise ValueError("successful usage charges require a receipt")
        if self.error_code is not None and (
            self.charge_id is not None or self.ledger_entry_id is not None or self.replayed
        ):
            raise ValueError("failed usage charges cannot expose committed fields")
        return self


class UsageRecordRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: UUID | None
    requested: str | Decimal
    replayed: StrictBool
    error_code: str | None

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if self.error_code is None and self.charge_id is None:
            raise ValueError("successful usage records require a charge receipt")
        if self.error_code is not None and (self.charge_id is not None or self.replayed):
            raise ValueError("failed usage records cannot expose committed fields")
        return self


class UsageRecordRpcRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: UUID | None
    requested: str | Decimal
    ledger_entry_id: UUID | None
    charged: str | Decimal
    allowance_covered: str | Decimal
    replayed: StrictBool
    error_code: str | None


class RefundRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_entry_id: UUID | None
    user_id: UUID | None
    amount: str | Decimal | None
    new_balance: str | Decimal | None
    bucket_breakdown: dict[str, str | Decimal] | None
    error: str | None

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if self.error is None and (
            self.refund_entry_id is None or self.user_id is None or self.amount is None or self.new_balance is None
        ):
            raise ValueError("successful refunds require identity and balance fields")
        if self.error is not None and (self.refund_entry_id is not None or self.bucket_breakdown is not None):
            raise ValueError("failed refunds cannot expose committed fields")
        return self


class RefundRpcRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: UUID | None
    subject_id: UUID | None
    amount: str | Decimal | None
    balance_after: str | Decimal | None
    replayed: StrictBool
    error_code: str | None


class RevokeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    entry_type: str
    revoked: str | Decimal
    balance_after: str | Decimal | None
    error_code: str | None

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if self.error_code is None and self.balance_after is None:
            raise ValueError("successful revocations require a committed balance")
        return self


class LeaseRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: UUID | None
    user_id: UUID
    amount: str | Decimal | None
    minimum_balance: str | Decimal | None
    expires_at: datetime | None
    error: str | None

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if self.error is None and (
            self.lease_id is None or self.amount is None or self.minimum_balance is None or self.expires_at is None
        ):
            raise ValueError("successful lease acquisition requires lease and policy fields")
        if self.error is not None and any(
            value is not None for value in (self.lease_id, self.amount, self.minimum_balance, self.expires_at)
        ):
            raise ValueError("failed lease mutations cannot expose committed fields")
        return self

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("lease expiry must include a timezone")
        return value


class LeaseMutationRpcRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: UUID | None
    status: Literal["active", "settling", "settled", "released", "expired"]
    reserved_amount: str | Decimal
    error_code: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.error_code is None and (self.lease_id is None or self.status != "active"):
            raise ValueError("successful lease mutations require an active lease")
        return self


class SettleLeaseRpcRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ledger_entry_id: UUID | None
    charge_id: UUID | None
    settled_amount: str | Decimal
    replayed: StrictBool
    error_code: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.error_code is None and self.charge_id is None:
            raise ValueError("successful lease settlement requires a charge receipt")
        if self.error_code is not None and (
            self.ledger_entry_id is not None or self.charge_id is not None or self.replayed
        ):
            raise ValueError("failed lease settlement cannot expose committed fields")
        return self


class LeasePricingContextRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_revision_no: int = Field(gt=0)
    plan_id: UUID | None
    plan_key: str | None
    rate_card: str | None

    @model_validator(mode="after")
    def validate_plan_identity(self) -> Self:
        if (self.plan_id is None) != (self.plan_key is None):
            raise ValueError("lease plan identity fields are inconsistent")
        return self


class ReleaseRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    released: StrictBool
    reason: Literal["active", "settling", "settled", "released", "expired", "missing_lease"] | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.released != (self.reason is None):
            raise ValueError("release outcome and reason are inconsistent")
        return self


class CatalogRevisionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    config: dict[str, Any]
    version: int
    label: str | None
    active: bool
    status: Literal["draft", "published", "active", "retired"]
    created_at: str | datetime


class EntitlementValueRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any


class AdmissionOperationRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_in_flight: int | None = Field(gt=0)


class EntitlementRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_key: str = Field(min_length=1)
    feature_type: Literal["boolean", "integer", "string", "enum"]
    feature_value: Any
    catalog_revision_id: UUID
    plan_key: str | None
    value_source: Literal["default", "plan"]


class UserPlanRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    plan_assigned_at: datetime
    plan_assignment_ends_at: datetime | None
    assignment_source_type: Literal["manual", "subscription", "migration", "system"]
    assignment_source_id: UUID | None
    catalog_revision_pinned: StrictBool
    plan_id: UUID
    plan_key: str = Field(min_length=1)
    plan_label: str = Field(min_length=1)
    rate_card: str | None
    allowed_operations: list[str]
    credit_allowance_amount: str | Decimal | None
    credit_allowance_priority: int | None = Field(ge=0)
    credit_allowance_reset_unit: Literal["second", "minute", "hour", "day", "week", "month", "year"] | None
    credit_allowance_reset_count: int | None = Field(gt=0)
    credit_allowance_reset_anchor: Literal["calendar", "plan_assignment", "rolling"] | None
    credit_allowance_reset_timezone: str | None
    entitlements: dict[str, EntitlementValueRow]
    credit_policy_type: Literal["prepaid", "credit_line"] | None
    credit_limit: str | Decimal | None
    admission_max_in_flight: int | None = Field(gt=0)
    operation_admission: dict[str, AdmissionOperationRow]
    catalog_revision_no: int = Field(gt=0)

    @field_validator("plan_assigned_at", "plan_assignment_ends_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("plan assignment timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_policies(self) -> Self:
        allowance = (
            self.credit_allowance_amount,
            self.credit_allowance_priority,
            self.credit_allowance_reset_unit,
            self.credit_allowance_reset_count,
            self.credit_allowance_reset_anchor,
            self.credit_allowance_reset_timezone,
        )
        populated = sum(value is not None for value in allowance)
        if populated not in (0, len(allowance)):
            raise ValueError("allowance policy fields must be all set or all null")
        if (
            (self.credit_policy_type is None and self.credit_limit is not None)
            or (self.credit_policy_type == "prepaid" and self.credit_limit is not None)
            or (self.credit_policy_type == "credit_line" and self.credit_limit is None)
        ):
            raise ValueError("credit policy fields are inconsistent")
        return self


class SetUserPlanRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    plan_id: UUID
    plan_key: str = Field(min_length=1)
    plan_assigned_at: datetime
    assignment_state: Literal["applied", "scheduled"]

    @field_validator("plan_assigned_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("plan assignment timestamp must include a timezone")
        return value


class PlanMigrationBatchRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migrated: int = Field(ge=0)
    done: StrictBool
    next_cursor: UUID | None


class AllowanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    allowance_remaining: str | Decimal
    period_start: datetime
    period_end: datetime

    @field_validator("period_start", "period_end")
    @classmethod
    def validate_period_timestamps(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("allowance timestamps must include a timezone")
        return value


class SpendByUserRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    total_spend: str | Decimal
    entry_count: int


class SpendByModelRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    total_spend: str | Decimal
    entry_count: int


class TopUserRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    total_spend: str | Decimal


class DailySpendRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str | date
    total_spend: str | Decimal
    entry_count: int


class AggregateStatsRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_credits_consumed: str | Decimal
    active_users: int
    avg_daily_spend: str | Decimal
    top_model: str | None
    top_user: str | None


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    account_id: str
    actor_user_id: str | None
    amount: str | Decimal
    entry_type: str
    operation: str
    reference_entry_id: str | None
    idempotency_key: str | None
    metadata: dict[str, Any] | None
    created_at: str | datetime


class UsageChargeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage_id: str
    account_id: str
    operation: str
    requested: str | Decimal
    charged: str | Decimal
    allowance_requested: str | Decimal
    allowance_covered: str | Decimal
    billing_disposition: Literal["billable", "record_only"]
    feature: str | None
    model: str | None
    region: str | None
    event_at: str | datetime
    idempotency_key: str
    metadata: dict[str, Any] | None
    created_at: str | datetime


class CreateTeamRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: UUID | None
    name: str | None
    team_subject_id: UUID | None
    account_id: UUID | None
    error_code: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        identity = (self.team_id, self.name, self.team_subject_id, self.account_id)
        if self.error_code is None and any(value is None for value in identity):
            raise ValueError("successful team creation requires identity fields")
        if self.error_code is not None and any(value is not None for value in identity):
            raise ValueError("failed team creation cannot expose identity fields")
        return self


class TeamBalanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: UUID
    name: str = Field(min_length=1)
    balance: str | Decimal
    member_count: int = Field(ge=0)


class AddTeamMemberRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: UUID
    user_id: UUID
    role: Literal["owner", "admin", "member"]


class TeamMemberRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["owner", "admin", "member"]
    spend_cap: str | Decimal | None
    total_spent: str | Decimal


class TeamDeductionRpcRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: UUID | None
    team_id: UUID
    subject_id: UUID
    amount: str | Decimal
    balance_after: str | Decimal | None
    replayed: StrictBool
    error_code: str | None


class TeamDeductionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: UUID | None
    team_id: UUID
    user_id: UUID
    amount: str | Decimal
    team_balance_after: str | Decimal | None
    replayed: StrictBool
    error: str | None

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if self.error is None and (self.entry_id is None or self.team_balance_after is None):
            raise ValueError("successful team deductions require entry and balance fields")
        if self.error is not None and (self.entry_id is not None or self.replayed):
            raise ValueError("failed team deductions cannot be replayed or committed")
        return self


class BucketBalanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    priority: int = Field(ge=0)
    expires: StrictBool
    balance: str | Decimal


class BucketEnvelopeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    buckets: list[BucketBalanceRow]
    total_balance: Decimal


class SweepRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expired_count: int = Field(ge=0)
    expired_amount: str | Decimal
    expired_by_bucket: dict[str, str | Decimal]


class QuotaStateRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    quota_key: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    measure_key: str = Field(min_length=1)
    quota_limit: str | Decimal
    consumed: str | Decimal
    reserved: str | Decimal
    remaining: str | Decimal
    overage: str | Decimal
    enforcement: Literal["block", "allow"]
    window_start: datetime
    window_end: datetime
    emit_at_percent: list[float]

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_window_timestamps(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("quota window timestamps must include a timezone")
        return value

    @field_validator("emit_at_percent")
    @classmethod
    def validate_thresholds(cls, value: list[float]) -> list[float]:
        if any(not 0 <= threshold <= 100 for threshold in value):
            raise ValueError("quota thresholds must be between 0 and 100")
        return value


class QuotaEventRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    quota_key: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    measure_key: str = Field(min_length=1)
    event_type: Literal["threshold", "blocked"]
    threshold_percent: float | None = Field(ge=0, le=100)
    idempotency_key: str = Field(min_length=1)
    usage_charge_id: UUID | None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("quota event timestamps must include a timezone")
        return value


class UnsetUserPlanRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class CatalogRevisionSummaryRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: int
    label: str | None
    active: bool
    created_at: str | datetime


class DeductParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    operation: str
    amount: str
    idempotency_key: str
    feature: str | None
    model: str | None
    region: str | None
    measures: str
    dimensions: str
    metadata: str


class CreateLeaseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    amount: str
    operation_type: str
    idempotency_key: str
    metadata: str
    ttl_seconds: int = 600
    feature: str | None = None
    measures: str
    dimensions: str
    minimum_balance: str | None = None
    max_concurrent: int | None = None


class SettleLeaseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    lease_id: str
    amount: str
    idempotency_key: str
    feature: str | None = None
    model: str | None = None
    region: str | None = None
    measures: str
    dimensions: str
    metadata: str
