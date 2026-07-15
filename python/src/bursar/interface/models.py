from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BillingMode = Literal["strict", "overdraft"]


class CreditMetadata(BaseModel, extra="allow"):
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    reference_type: str | None = None
    reference_id: str | None = None
    idempotency_key: str | None = None
    flat_job: str | None = None


class BalanceResult(BaseModel):
    user_id: str
    balance: Decimal = Decimal(0)
    lifetime_purchased: Decimal = Decimal(0)


class AddCreditsResult(BaseModel):
    transaction_id: str
    user_id: str
    amount: Decimal
    new_balance: Decimal
    lifetime_purchased: Decimal = Decimal(0)
    bucket: str = "default"


class LeaseResult(BaseModel):
    lease_id: str
    user_id: str
    amount: Decimal = Decimal(0)
    available: Decimal = Decimal(0)
    reserved_total: Decimal = Decimal(0)
    billing_mode: BillingMode = "strict"
    expires_at: datetime | None = None
    error: str | None = None


class ReleaseResult(BaseModel):
    lease_id: str
    user_id: str
    released: bool = False
    reason: str | None = None


class CanAffordResult(BaseModel):
    affordable: bool = False
    spendable: Decimal = Decimal(0)
    worst_case: Decimal = Decimal(0)
    reason: str | None = None


class AvailableResult(BaseModel):
    user_id: str
    balance: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    available: Decimal = Decimal(0)


class DeductionResult(BaseModel):
    transaction_id: str
    user_id: str
    amount: Decimal
    balance_after: Decimal
    allowance_consumed: Decimal = Decimal(0)
    idempotent: bool = False
    cap_warning: str | None = None
    feature_limit_warning: str | None = None
    error: str | None = None
    bucket_breakdown: dict[str, Decimal] | None = None


class BursarConfigResult(BaseModel):
    id: str
    config: dict[str, Any] | None = None
    version: int = 1
    publication_version: int | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _sync_publication_version(self) -> BursarConfigResult:
        if self.publication_version is None:
            self.publication_version = self.version
        return self


class BursarConfigHistoryItem(BaseModel):
    id: str
    version: int
    label: str | None = None
    active: bool = False
    created_at: str = ""


class SetupResult(BaseModel):
    tables_created: list[str] = Field(default_factory=list)
    rpcs_created: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class OperationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_mode: BillingMode = "strict"
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None


class Entitlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any | None = None
    max_calls: int | None = Field(default=None, ge=0)
    period: Literal["daily", "weekly", "monthly", "yearly"] = "monthly"
    on_exceed: Literal["deny", "warn", "notify"] = "deny"


class Allowance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(default=Decimal(0), ge=0)
    period: Literal["calendar_month", "rolling_30d", "anniversary"] = "calendar_month"


class PlanSafety(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_mode: BillingMode = "strict"
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    per_operation: dict[str, OperationPolicy] | None = None


class PlanDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    allowance: Allowance | None = None
    rate_overrides: dict[str, str] | None = None
    safety: PlanSafety | None = None
    entitlements: dict[str, Entitlement] | None = None


class BucketDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ""
    priority: int = 0
    expires: bool = False
    ttl_days: int | None = None
    default: bool = False
    allow_overdraft: bool = False

    @model_validator(mode="after")
    def _derive_expires(self) -> BucketDefinition:
        if self.ttl_days is not None:
            self.expires = True
        return self


class AllowanceResult(BaseModel):
    plan_id: str
    allowance_remaining: Decimal
    period_start: date | None = None
    period_end: date | None = None


class GetUserPlanResult(BaseModel):
    user_id: str
    plan_id: str | None = None
    plan_label: str | None = None
    allowance_amount: Decimal = Decimal(0)
    allowance_period: Literal["calendar_month", "rolling_30d", "anniversary"] = "calendar_month"
    entitlements: dict[str, Entitlement] = Field(default_factory=dict)
    rate_overrides: dict[str, str] = Field(default_factory=dict)
    billing_mode: BillingMode = "strict"
    per_operation: dict[str, OperationPolicy] = Field(default_factory=dict)
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    plan_assigned_at: datetime | None = None
    config_version: int | None = None
    catalog_version: int | None = None


class FeatureLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls: int
    period: Literal["daily", "weekly", "monthly", "yearly"] = "monthly"
    action: Literal["deny", "warn", "notify"] = "deny"


class CheckFeatureResult(BaseModel):
    user_id: str
    feature: str
    value: Any = None
    has_feature: bool = False


class FeatureLimitResult(BaseModel):
    user_id: str
    feature: str
    limited: bool = False
    limit: int = 0
    used: int = 0
    remaining: int = 0
    period_start: date | None = None
    period_end: date | None = None
    action: Literal["deny", "warn", "notify"] | None = None


class SetUserPlanResult(BaseModel):
    user_id: str
    plan_id: str
    plan_assigned_at: datetime | None = None


class MigratePlanUsersResult(BaseModel):
    plan_key: str
    target_plan_id: str
    target_config_version: int
    migrated_count: int


class RefundResult(BaseModel):
    refund_transaction_id: str
    original_transaction_id: str
    user_id: str
    amount: Decimal = Decimal(0)
    new_balance: Decimal = Decimal(0)
    error: str | None = None
    bucket_breakdown: dict[str, Decimal] | None = None


class SweepResult(BaseModel):
    expired_count: int = 0
    expired_amount: Decimal = Decimal(0)
    dry_run: bool = False
    expired_by_bucket: dict[str, Decimal] | None = None


class BucketBalance(BaseModel):
    bucket_key: str
    label: str = ""
    priority: int = 0
    expires: bool = False
    balance: Decimal = Decimal(0)


class BucketBalancesResult(BaseModel):
    user_id: str
    buckets: list[BucketBalance]
    total_balance: Decimal


class SpendByUserRow(BaseModel):
    user_id: str = ""
    total_spend: Decimal = Decimal(0)
    transaction_count: int = 0


class SpendByModelRow(BaseModel):
    model: str = ""
    total_spend: Decimal = Decimal(0)
    transaction_count: int = 0


class TopUserRow(BaseModel):
    user_id: str = ""
    total_spend: Decimal = Decimal(0)


class DailySpendRow(BaseModel):
    date: str = ""
    total_spend: Decimal = Decimal(0)
    transaction_count: int = 0


class AggregateStatsRow(BaseModel):
    total_credits_consumed: Decimal = Decimal(0)
    active_users: int = 0
    avg_daily_spend: Decimal = Decimal(0)
    top_model: str = ""
    top_user: str = ""


class TransactionRow(BaseModel):
    id: str = ""
    user_id: str = ""
    amount: Decimal = Decimal(0)
    type: str = ""
    reference_type: str | None = None
    reference_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str = ""
    total_count: int = 0


class SpendCap(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = ""
    cap_type: Literal["daily", "monthly"] = Field(default="daily", alias="type")
    model: str | None = None
    limit: Decimal = Field(default=Decimal(0), ge=0)
    action: Literal["deny", "warn", "notify"] = "deny"


class CapCheckResult(BaseModel):
    capped: bool = False
    current_spend: Decimal = Decimal(0)
    cap_limit: Decimal = Decimal(0)
    action: Literal["deny", "warn", "notify"] | None = None
    model: str | None = None


class Team(BaseModel):
    team_id: str = ""
    name: str = ""
    balance: Decimal = Decimal(0)
    member_count: int = 0
    created_at: str = ""


class TeamBalanceResult(BaseModel):
    team_id: str = ""
    name: str = ""
    balance: Decimal = Decimal(0)
    member_count: int = 0


class TeamMember(BaseModel):
    user_id: str = ""
    role: str = ""
    spend_cap: Decimal | None = None
    total_spent: Decimal = Decimal(0)


class CreateTeamResult(BaseModel):
    team_id: str = ""
    name: str = ""


class AddTeamMemberResult(BaseModel):
    team_id: str = ""
    user_id: str = ""
    role: str = "member"


class TeamDeductionResult(BaseModel):
    transaction_id: str = ""
    team_id: str = ""
    user_id: str = ""
    amount: Decimal = Decimal(0)
    team_balance_after: Decimal = Decimal(0)
    error: str | None = None
