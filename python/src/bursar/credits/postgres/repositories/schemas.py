"""Pydantic models for raw database row shapes returned by repositories."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


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
    entry_id: str = ""
    amount: str | Decimal | None = None
    balance_after: str | Decimal | None = None
    allowance_consumed: str | Decimal | None = None
    idempotent: bool = False
    cap_warning: str | None = None
    feature_limit_warning: str | None = None
    bucket_breakdown: dict[str, str | Decimal] | None = None
    error: str | None = None
    user_id: str = ""


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


class ActivePricingRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    config: dict[str, Any] | None = None
    version: int = 0
    label: str | None = None
    active: bool = False
    created_at: str | datetime = ""
    error: str | None = None


class UserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_id: str | None = None
    plan_key: str | None = None
    plan_label: str | None = None
    allowance_amount: str | Decimal | None = None
    allowance_period: str = "calendar_month"
    entitlements: dict[str, Any] | None = None
    rate_overrides: dict[str, Any] | None = None
    billing_mode: str = "strict"
    per_operation: dict[str, Any] | None = None
    max_concurrent: int | None = None
    overdraft_floor: str | Decimal | None = None
    plan_assigned_at: str | datetime | None = None
    config_version: int | None = None
    catalog_version: int | None = None


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


class PlanMigrationUsersRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    plan_key: str
    target_plan_id: str
    target_config_version: int
    migrated_count: int


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
    reference_entry_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | datetime = ""
    next_cursor_created_at: str | datetime | None = None
    next_cursor_entry_id: str | None = None


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
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    plan_id: str | None = None
    offer_key: str = ""
    plan: str | None = None
    interval: str = ""
    interval_count: int = 0
    grant_mode: str | None = None
    grant_credits: str | Decimal | None = None
    grant_bucket: str | None = None
    grant_replace_prior: bool = False


class BillingTopupRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    topup_key: str = ""
    credits_per_unit: str | Decimal | None = None
    credits_per_major_unit: str | Decimal | None = None
    tier: str = ""
    deposit_to: str = ""
    bucket_key: str | None = None
    amount_minor: int | str | None = None
    currency: str | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    default_quantity: int | None = None


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
    event_id: str = ""
    provider: str = ""
    status: str = "retry"
    claim_token: str | None = None


class BillingPaymentRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    provider: str = ""
    provider_payment_id: str = ""
    user_id: str | None = None
    amount_minor: int = 0
    tax_minor: int | None = None
    currency: str = "USD"
    purpose: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | datetime | None = None
    updated_at: str | datetime | None = None
    credits_per_unit: str | Decimal | None = None
    credits_per_major_unit: str | Decimal | None = None


class UnsetUserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_key: str | None = None


class BursarConfigHistoryItemRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    version: int = 0
    label: str | None = None
    active: bool = False
    created_at: str = ""


class DeductParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = ""
    amount: str = "0"
    idempotency_key: str | None = None
    min_balance: str = "0"
    model: str | None = None
    metadata: str = "{}"
    skip_allowance: bool = False
    period_start: str | None = None
    feature: str | None = None
    feature_max_calls: int | None = None
    feature_action: str | None = None
    feature_period_start: str | None = None
    feature_period_end: str | None = None


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
