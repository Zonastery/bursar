"""Pydantic schemas for credit store operations.

All store methods accept and return typed Pydantic models rather than
raw dicts — validation at the boundary, clarity in the call sites.

Money is represented as :class:`decimal.Decimal` everywhere (contract §1):
credits are fractional and must never be computed in binary float. Quantization
to 4 dp with ``ROUND_HALF_UP`` happens at the money boundary (manager/engine and
on persistence), not inside these models.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── Metadata ──────────────────────────────────────────────────────────


class CreditMetadata(BaseModel, extra="allow"):
    """Flexible metadata attached to credit transactions.

    Known fields are typed; arbitrary extras pass through to JSONB.

    Merge order (contract §5, M7): the MANAGER merges caller metadata **first**
    and system-seeded fields **last**, so the system-owned reserved keys
    (``idempotency_key``, ``model``, ``breakdown_total``) always win over
    caller-supplied values. This model keeps ``extra="allow"`` so callers can
    attach arbitrary keys; it does not block that merge order (the manager owns
    the merge). Reserved system keys must not be overwritten by caller metadata.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    reference_type: str | None = None
    reference_id: str | None = None
    idempotency_key: str | None = None
    fixed_job: str | None = None


# ── Pricing configuration ─────────────────────────────────────────────


class PricingConfigData(BaseModel):
    """Pricing configuration schema.

    Mirrors the YAML config structure used by ``PricingEngine``.
    Unified format with optional plan definitions.
    """

    version: Literal[1] = 1
    models: dict[str, str]
    tools: dict[str, str] = Field(default_factory=lambda: {"_default": "tool_calls * 0"})
    search: dict[str, str] = Field(default_factory=dict)
    cache: dict[str, str] = Field(default_factory=dict)
    fixed: dict[str, int] = Field(default_factory=dict)
    min_balance: Decimal = Field(default=Decimal(5), ge=0)
    plans: dict[str, PlanDefinition] | None = None


# ── Runtime results ───────────────────────────────────────────────────


class BalanceResult(BaseModel):
    """Current credit balance for a user."""

    user_id: str
    balance: Decimal = Decimal(0)
    lifetime_purchased: Decimal = Decimal(0)


class AddCreditsResult(BaseModel):
    """Result of adding credits to a user's account."""

    transaction_id: str
    user_id: str
    amount: Decimal
    new_balance: Decimal
    lifetime_purchased: Decimal = Decimal(0)


class ReserveResult(BaseModel):
    """Result of reserving credits for an operation."""

    reservation_id: str
    user_id: str
    amount: Decimal
    balance: Decimal = Decimal(0)
    reserved_total: Decimal = Decimal(0)
    error: str | None = None


class DeductionResult(BaseModel):
    """Result of deducting credits after an operation completes.

    ``amount`` is the net amount charged to the balance (gross cost minus any
    free allowance consumed). ``allowance_consumed`` is the portion covered by
    free allowance, and ``cap_warning`` carries a non-blocking ``warn``/``notify``
    spend-cap signal (``None`` when no cap fired). On failure, ``error`` carries
    a business code (e.g. ``insufficient_credits``, ``cap_reached``) for the
    manager to map to a typed exception.
    """

    transaction_id: str
    user_id: str
    amount: Decimal
    balance_after: Decimal
    allowance_consumed: Decimal = Decimal(0)
    idempotent: bool = False
    cap_warning: str | None = None
    error: str | None = None


class PricingConfigResult(BaseModel):
    """Versioned pricing configuration fetched from the store."""

    id: str
    config: PricingConfigData
    version: int = 1
    label: str | None = None


class PricingConfigHistoryItem(BaseModel):
    """Lightweight summary for pricing version listing."""

    id: str
    version: int
    label: str | None = None
    active: bool = False
    created_at: str = ""


class SetupResult(BaseModel):
    """Report of what the setup step created or updated."""

    tables_created: list[str] = Field(default_factory=list)
    rpcs_created: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


# ── Plan types ─────────────────────────────────────────────────────────


class PlanDefinition(BaseModel):
    """Definition of a subscription plan with free allowance and rate overrides."""

    id: str
    name: str
    free_allowance: Decimal = Field(default=Decimal(0), ge=0)
    rate_overrides: dict[str, str] | None = None
    features: dict[str, Any] | None = None


class AllowanceResult(BaseModel):
    """Result of checking plan allowance."""

    plan_id: str
    allowance_remaining: Decimal
    period_start: str
    period_end: str


class GetUserPlanResult(BaseModel):
    """Result of fetching a user's current plan."""

    user_id: str
    plan_id: str | None = None
    plan_name: str | None = None
    free_allowance: Decimal = Decimal(0)
    features: dict[str, Any] = Field(default_factory=dict)


class CheckFeatureResult(BaseModel):
    """Result of checking a user's feature entitlement."""

    user_id: str
    feature: str
    value: Any = None
    has_feature: bool = False


class SetUserPlanResult(BaseModel):
    """Result of assigning a plan to a user."""

    user_id: str
    plan_id: str


class RefundResult(BaseModel):
    """Result of refunding a credit deduction.

    On failure, ``error`` carries a business code (e.g. ``already_refunded``,
    ``over_refund``, ``not_found``) so the manager can reject over-refunds and
    duplicates before emitting a success event (contract §4).
    """

    refund_transaction_id: str
    original_transaction_id: str
    user_id: str
    amount: Decimal = Decimal(0)
    new_balance: Decimal = Decimal(0)
    error: str | None = None


class SweepResult(BaseModel):
    """Result of sweeping expired credits."""

    expired_count: int = 0
    expired_amount: Decimal = Decimal(0)
    dry_run: bool = False


# ── Usage analytics ──────────────────────────────────────────────────────


class SpendByUserRow(BaseModel):
    """Aggregated spend for a single user in a time window."""

    user_id: str = ""
    total_spend: Decimal = Decimal(0)
    transaction_count: int = 0


class SpendByModelRow(BaseModel):
    """Aggregated spend for a single model in a time window."""

    model: str = ""
    total_spend: Decimal = Decimal(0)
    transaction_count: int = 0


class TopUserRow(BaseModel):
    """Top-spending user in a time window."""

    user_id: str = ""
    total_spend: Decimal = Decimal(0)


class DailySpendRow(BaseModel):
    """Daily spend aggregation in a time window."""

    date: str = ""
    total_spend: Decimal = Decimal(0)
    transaction_count: int = 0


class AggregateStatsRow(BaseModel):
    """Aggregate statistics across all users in a time window."""

    total_credits_consumed: Decimal = Decimal(0)
    active_users: int = 0
    avg_daily_spend: Decimal = Decimal(0)
    top_model: str = ""
    top_user: str = ""


# ── Transaction listing ────────────────────────────────────────────────────


class TransactionRow(BaseModel):
    """A single credit transaction row."""

    id: str = ""
    user_id: str = ""
    amount: Decimal = Decimal(0)
    type: str = ""
    reference_type: str | None = None
    reference_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str = ""
    total_count: int = 0


# ── Spend caps and rate limiting ───────────────────────────────────────


class SpendCap(BaseModel):
    """Configuration for a per-user spend cap.

    ``populate_by_name=True`` so the field accepts both its name (``cap_type``)
    and its alias (``type``) on input, standardizing (de)serialization across
    the camelCase/snake_case boundary (contract §5, M14-models).
    """

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = ""
    cap_type: Literal["daily", "monthly"] = Field(default="daily", alias="type")
    model: str | None = None
    limit: Decimal = Field(default=Decimal(0), ge=0)
    action: Literal["deny", "warn", "notify"] = "deny"


class CapCheckResult(BaseModel):
    """Result of checking a spend cap.

    ``action`` is ``None`` when no cap applies; consumers default-**deny** on an
    unknown/None action (contract §5, M8).
    """

    capped: bool = False
    current_spend: Decimal = Decimal(0)
    cap_limit: Decimal = Decimal(0)
    action: Literal["deny", "warn", "notify"] | None = None
    model: str | None = None


# ── Team/shared balance pools ─────────────────────────────────────────


class Team(BaseModel):
    """A team with a shared credit balance pool."""

    team_id: str = ""
    name: str = ""
    balance: Decimal = Decimal(0)
    member_count: int = 0
    created_at: str = ""


class TeamBalanceResult(BaseModel):
    """Result of fetching team balance."""

    team_id: str = ""
    name: str = ""
    balance: Decimal = Decimal(0)
    member_count: int = 0


class TeamMember(BaseModel):
    """A member of a team with optional spend cap."""

    user_id: str = ""
    role: str = ""
    spend_cap: Decimal | None = None
    total_spent: Decimal = Decimal(0)


class CreateTeamResult(BaseModel):
    """Result of creating a team."""

    team_id: str = ""
    name: str = ""


class AddTeamMemberResult(BaseModel):
    """Result of adding a team member."""

    team_id: str = ""
    user_id: str = ""
    role: str = "member"


class TeamDeductionResult(BaseModel):
    """Result of deducting credits from a team pool."""

    transaction_id: str = ""
    team_id: str = ""
    user_id: str = ""
    amount: Decimal = Decimal(0)
    team_balance_after: Decimal = Decimal(0)
    error: str | None = None
