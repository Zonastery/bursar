"""Pydantic models for raw database row shapes returned by repositories."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BalanceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    balance: str | Decimal | None = None
    lifetime_purchased: str | Decimal | None = None


class AddCreditsRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entry_id: str | None = None
    user_id: str = ""
    amount: str | Decimal | None = None
    new_balance: str | Decimal | None = None
    lifetime_purchased: str | Decimal | None = None
    bucket: str = "default"
    idempotent: bool = False
    error: str | None = None


class AvailableRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    balance: str | Decimal | None = None
    reserved: str | Decimal | None = None
    available: str | Decimal | None = None


class GrantProgramAwardRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    grant_event_id: str | None = None
    grant_award_id: str | None = None
    recipient_subject_id: str | None = None
    ledger_entry_id: str | None = None
    amount: str | Decimal | None = None
    replayed: bool = False
    error_code: str | None = None


class DeductionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    charge_id: str | None = None
    # Allowance-only and zero-cost usage has no monetary ledger entry.
    entry_id: str | None = None
    amount: str | Decimal | None = None
    balance_after: str | Decimal | None = None
    allowance_consumed: str | Decimal | None = None
    idempotent: bool = False
    bucket_breakdown: dict[str, str | Decimal] | None = None
    error: str | None = None
    user_id: str = ""


class UsageRecordRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    charge_id: str | None = None
    requested: str | Decimal | None = None
    replayed: bool = False
    error_code: str | None = None


class RefundRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    refund_entry_id: str = ""
    user_id: str = ""
    amount: str | Decimal | None = None
    new_balance: str | Decimal | None = None
    bucket_breakdown: dict[str, str | Decimal] | None = None
    error: str | None = None


class RevokeRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    amount: str | Decimal | None = None
    new_balance: str | Decimal | None = None
    bucket: str | None = None
    error: str | None = None


class LeaseRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lease_id: str = ""
    user_id: str = ""
    amount: str | Decimal | None = None
    available: str | Decimal | None = None
    reserved: str | Decimal | None = None
    billing_mode: str = "strict"
    minimum_balance: str | Decimal | None = None
    expires_at: datetime | None = None
    error: str | None = None


class LeasePricingContextRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    catalog_revision_no: int
    plan_id: str | None = None
    plan_key: str | None = None
    rate_card: str | None = None


class ReleaseRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    released: bool = False
    reason: str | None = None


class CatalogRevisionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    config: dict[str, Any]
    version: int
    label: str | None = None
    active: bool
    created_at: str | datetime
    error: str | None = None


class UserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_id: str | None = None
    plan_key: str | None = None
    plan_label: str | None = None
    rate_card: str | None = None
    credit_allowance_amount: str | Decimal | None = None
    credit_allowance_priority: int | None = None
    credit_allowance_reset_unit: str | None = None
    credit_allowance_reset_count: int | None = None
    credit_allowance_reset_anchor: str | None = None
    credit_allowance_reset_timezone: str | None = None
    entitlements: dict[str, Any] | None = None
    credit_policy_type: str | None = None
    credit_limit: str | Decimal | None = None
    admission_max_in_flight: int | None = None
    operation_admission: dict[str, Any] | None = None
    allowed_operations: list[str] | None = None
    assignment_source_type: str | None = None
    assignment_source_id: str | None = None
    catalog_revision_pinned: bool = False
    plan_assigned_at: str | datetime | None = None
    catalog_revision_no: int | None = None


class SetUserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_id: str = ""
    plan_assigned_at: str | datetime | None = None


class PlanMigrationBatchRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    migrated: int = 0
    done: bool = False
    next_cursor: str | None = None


class AllowanceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    plan_id: str | None = None
    allowance_amount: str | Decimal | None = None
    allowance_remaining: str | Decimal | None = None
    period_start: str | datetime | date | None = None
    period_end: str | datetime | date | None = None


class CapCheckRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    capped: bool = False
    current_spend: str | Decimal | None = None
    cap_limit: str | Decimal | None = None
    action: str | None = None
    model: str | None = None


class SpendByUserRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    total_spend: str | Decimal | None = None
    entry_count: int = 0


class SpendByModelRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = ""
    total_spend: str | Decimal | None = None
    entry_count: int = 0


class TopUserRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    total_spend: str | Decimal | None = None


class DailySpendRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str = ""
    total_spend: str | Decimal | None = None
    entry_count: int = 0


class AggregateStatsRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_credits_consumed: str | Decimal | None = None
    active_users: int = 0
    avg_daily_spend: str | Decimal | None = None
    top_model: str = ""
    top_user: str = ""


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entry_id: str = ""
    account_id: str = ""
    actor_user_id: str | None = None
    amount: str | Decimal | None = None
    entry_type: str = ""
    operation: str = ""
    reference_entry_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | datetime = ""
    next_cursor_created_at: str | datetime | None = None
    next_cursor_entry_id: str | None = None


class UsageChargeRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    usage_id: str = ""
    account_id: str = ""
    operation: str = ""
    requested: str | Decimal | None = None
    charged: str | Decimal | None = None
    allowance_requested: str | Decimal | None = None
    allowance_covered: str | Decimal | None = None
    billing_disposition: Literal["billable", "record_only"] = "billable"
    feature: str | None = None
    model: str | None = None
    region: str | None = None
    event_at: str | datetime = ""
    idempotency_key: str = ""
    metadata: dict[str, Any] | None = None
    created_at: str | datetime = ""


class CreateTeamRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    team_id: str = ""
    name: str = ""
    error_code: str | None = None


class TeamBalanceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    team_id: str = ""
    name: str = ""
    balance: str | Decimal | None = None
    member_count: int = 0
    error: str | None = None


class AddTeamMemberRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    team_id: str = ""
    user_id: str = ""
    role: str = "member"
    error: str | None = None


class TeamMemberRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    role: str = "member"
    spend_cap: str | Decimal | None = None
    total_spent: str | Decimal | None = None


class TeamDeductionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entry_id: str = ""
    team_id: str = ""
    user_id: str = ""
    amount: str | Decimal | None = None
    team_balance_after: str | Decimal | None = None
    error: str | None = None


class BucketEnvelopeRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    buckets: list[dict[str, Any]] | None = None
    total_balance: str | Decimal | None = None
    error: str | None = None


class SweepRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    expired_count: int = 0
    expired_amount: str | Decimal | None = None
    dry_run: bool = False
    expired_by_bucket: dict[str, Any] | None = None


class QuotaStateRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: str
    quota_key: str
    operation_key: str
    measure_key: str
    quota_limit: str | Decimal
    consumed: str | Decimal
    reserved: str | Decimal
    remaining: str | Decimal
    overage: str | Decimal
    enforcement: Literal["block", "allow"]
    window_start: str
    window_end: str
    emit_at_percent: list[float]


class QuotaEventRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: str = ""
    quota_key: str = ""
    operation_key: str = ""
    measure_key: str = ""
    event_type: Literal["threshold", "blocked"]
    threshold_percent: float | None = None
    idempotency_key: str = ""
    usage_charge_id: str | None = None
    created_at: str | datetime = ""


# Billing schemas


class BillingOfferRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    plan_id: UUID
    offer_key: str = Field(min_length=1)
    plan: str = Field(min_length=1)
    interval: Literal["day", "week", "month", "year"]
    interval_count: int = Field(gt=0)
    grant_mode: Literal["cycle_grant"] | None
    grant_credits: Decimal | None = Field(gt=0)
    grant_bucket: str | None = Field(min_length=1)
    grant_replace_prior: bool

    @model_validator(mode="after")
    def validate_cycle_grant(self) -> Self:
        grant_fields = (self.grant_mode, self.grant_credits, self.grant_bucket)
        if any(value is None for value in grant_fields) != all(value is None for value in grant_fields):
            raise ValueError("cycle grant fields must either all be set or all be null")
        if self.grant_mode is None and self.grant_replace_prior:
            raise ValueError("grant_replace_prior requires a cycle grant")
        return self


class BillingTopupRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class SubscriptionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    user_id: str = ""
    provider: str = ""
    provider_subscription_id: str = ""
    provider_customer_id: str | None = None
    offer_id: str | None = None
    offer_key: str | None = None
    plan: str | None = None
    status: str = "incomplete"
    current_period_start: str | datetime | None = None
    current_period_end: str | datetime | None = None
    trial_end: str | datetime | None = None
    cancel_at: str | datetime | None = None
    ended_at: str | datetime | None = None
    cancel_at_period_end: bool = False
    interval: str | None = None
    interval_count: int | None = None
    grace_ends_at: str | datetime | None = None
    grace_expired_at: str | datetime | None = None
    provider_updated_at: str | datetime | None = None
    metadata: dict[str, Any] | None = None
    catalog_version: int | None = None
    plan_version_id: str | None = None


class BillingEventRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: str | None
    status: Literal[
        "claimed",
        "duplicate",
        "busy",
        "invalid_request",
        "idempotency_conflict",
        "max_retries_exceeded",
    ]
    claim_token: str | None = None


class BillingPaymentRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
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


class UnsetUserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_key: str | None = None


class CatalogRevisionSummaryRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    version: int
    label: str | None = None
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
    user_id: str = ""
    amount: str = "0"
    operation_type: str = ""
    idempotency_key: str
    metadata: str = "{}"
    ttl_seconds: int = 600
    feature: str | None = None
    measures: str = "{}"
    dimensions: str = "{}"
    minimum_balance: str | None = None
    max_concurrent: int | None = None


class SettleLeaseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = ""
    lease_id: str = ""
    amount: str = "0"
    idempotency_key: str
    feature: str | None = None
    model: str | None = None
    region: str | None = None
    measures: str = "{}"
    dimensions: str = "{}"
    metadata: str = "{}"
