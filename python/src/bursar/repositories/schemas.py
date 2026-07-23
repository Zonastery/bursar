"""Pydantic models for raw database row shapes returned by repositories."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class BalanceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    balance: str | Decimal | None = None
    lifetime_purchased: str | Decimal | None = None


class AddCreditsRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
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


class DeductionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    transaction_id: str = ""
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
    refund_transaction_id: str = ""
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
    expires_at: str = ""
    error: str | None = None


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
    created_at: str = ""
    error: str | None = None


class UserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_id: str | None = None
    plan_label: str | None = None
    allowance_amount: str | Decimal | None = None
    allowance_period: str = "calendar_month"
    entitlements: dict[str, Any] | None = None
    rate_overrides: dict[str, Any] | None = None
    billing_mode: str = "strict"
    per_operation: dict[str, Any] | None = None
    max_concurrent: int | None = None
    overdraft_floor: str | Decimal | None = None
    plan_assigned_at: str | None = None
    config_version: int | None = None
    catalog_version: int | None = None


class SetUserPlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    plan_id: str = ""
    plan_assigned_at: str | None = None


class MigratePlanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    plan_key: str = ""
    target_plan_id: str = ""
    target_config_version: int = 0
    migrated_count: int = 0
    error: str | None = None


class AllowanceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    plan_id: str | None = None
    allowance_amount: str | Decimal | None = None
    allowance_remaining: str | Decimal | None = None
    period_start: str | datetime | date | None = None
    period_end: str | datetime | date | None = None


class FeatureLimitRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    feature: str = ""
    limited: bool = False
    limit: int = 0
    used: int = 0
    remaining: int = 0
    period_start: str = ""
    period_end: str = ""
    action: str | None = None


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
    transaction_count: int = 0


class SpendByModelRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = ""
    total_spend: str | Decimal | None = None
    transaction_count: int = 0


class TopUserRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    total_spend: str | Decimal | None = None


class DailySpendRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str = ""
    total_spend: str | Decimal | None = None
    transaction_count: int = 0


class AggregateStatsRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_credits_consumed: str | Decimal | None = None
    active_users: int = 0
    avg_daily_spend: str | Decimal | None = None
    top_model: str = ""
    top_user: str = ""


class TransactionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    user_id: str = ""
    amount: str | Decimal | None = None
    type: str = ""
    reference_type: str | None = None
    reference_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | datetime = ""
    total_count: int = 0
    next_cursor_created_at: str | datetime | None = None
    next_cursor_id: str | None = None


class CreateTeamRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    team_id: str = ""
    name: str = ""
    error: str | None = None


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
    transaction_id: str = ""
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
    expired_by_bucket: dict[str, str | Decimal] | None = None
    error: str | None = None


# Billing schemas


class BillingOfferRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    offer_key: str = ""
    plan: str | None = None
    interval: str = ""
    interval_count: int = 0
    grant_mode: str = ""
    grant_credits: str | Decimal | None = None
    grant_bucket: str | None = None
    grant_replace_prior: bool = False


class BillingTopupRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topup_key: str = ""
    credits_per_unit: str | Decimal | None = None
    credits_per_major_unit: str | Decimal | None = None
    tier: str = ""
    deposit_to: str = ""


class SubscriptionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = ""
    provider: str = ""
    provider_subscription_id: str = ""
    provider_customer_id: str | None = None
    offer_key: str | None = None
    plan: str | None = None
    status: str = "incomplete"
    current_period_start: str | datetime | None = None
    current_period_end: str | datetime | None = None
    cancel_at_period_end: bool = False
    interval: str | None = None
    interval_count: int | None = None
    grace_ends_at: str | datetime | None = None
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
    provider: str = ""
    provider_payment_id: str = ""
    user_id: str | None = None
    amount_minor: int = 0
    tax_minor: int | None = None
    currency: str = "USD"
    purpose: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
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
    billing_mode: str = ""
    floor: str = "0"
    ceiling: str = "0"
    model: str | None = None
    metadata: str = "{}"
    ttl_seconds: int = 600
    skip_allowance: bool = False
    period_start: str | None = None
    feature: str | None = None
    feature_max_calls: int | None = None
    feature_action: str | None = None
    feature_period_start: str | None = None
    feature_period_end: str | None = None
    max_concurrent: str | None = None
    overdraft_floor: str | None = None


class SettleLeaseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = ""
    lease_id: str = ""
    amount: str = "0"
    metadata: str = "{}"
    billing_mode: str = ""
    skip_allowance: bool = False
    period_start: str | None = None
    feature: str | None = None
    feature_max_calls: int | None = None
    feature_action: str | None = None
    feature_period_start: str | None = None
    feature_period_end: str | None = None
    idempotency_key: str | None = None
    min_balance: str = "0"
    model: str | None = None
