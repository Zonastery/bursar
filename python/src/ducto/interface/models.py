"""Pydantic schemas for credit store operations.

All store methods accept and return typed Pydantic models rather than
raw dicts — validation at the boundary, clarity in the call sites.

Money is represented as :class:`decimal.Decimal` everywhere (contract §1):
credits are fractional and must never be computed in binary float. Quantization
to 4 dp with ``ROUND_HALF_UP`` happens at the money boundary (manager/engine and
on persistence), not inside these models.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Billing mode for an operation. ``strict`` never lets the balance fall below
#: the floor at admission (lease worst-case ⇒ zero debt); ``overdraft`` permits
#: the balance to go negative down to a configured floor and always bills the
#: full actual cost at settle (interface plan §1/D3/D5).
BillingMode = Literal["strict", "overdraft"]

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
    search: str | None = None
    cache: str | None = None
    fixed: dict[str, Decimal] = Field(default_factory=dict)
    min_balance: Decimal = Field(default=Decimal(0), ge=0)
    signup_bonus: int = 50
    plans: dict[str, PlanDefinition] | None = None
    tiers: dict[str, TierDefinition] | None = None


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
    tier: str = "default"


class LeaseResult(BaseModel):
    """Result of acquiring (or renewing) a lease — the atomic admission hold.

    A lease is the *only* admission control (interface plan §3/D4): it holds
    ``amount`` against ``available = balance − Σ(active holds)`` under one lock so
    concurrent operations see each other and ``max_concurrent`` is real. On failure
    ``error`` carries a business code (``insufficient_credits``, ``concurrency_limit``,
    ``cap_reached``, ``feature_not_entitled``, ``invalid_amount``, ``lease_not_found``,
    ``lease_expired``, ``lease_released``) for the manager to map to a typed exception.
    """

    lease_id: str
    user_id: str
    amount: Decimal = Decimal(0)
    available: Decimal = Decimal(0)
    reserved_total: Decimal = Decimal(0)
    billing_mode: BillingMode = "strict"
    expires_at: str = ""
    error: str | None = None


class ReleaseResult(BaseModel):
    """Result of releasing a lease without charging (interface plan §3).

    Idempotent and safe on missing/already-finalized leases: ``released`` is
    ``True`` only when this call transitioned an active/expired lease to released.
    ``reason`` is one of ``released``, ``already_released``, ``already_settled``,
    ``not_found`` — never a bare void (resolves H1).
    """

    lease_id: str
    user_id: str
    released: bool = False
    reason: str | None = None


class CanAffordResult(BaseModel):
    """Advisory affordability check — UI only, non-locking, may be stale (D4/H3).

    Never used for admission control; that is exclusively the lease (``reserve``).

    **Semantic note (#8):** ``spendable`` here is the *effective* spending power::

        spendable = balance − active_holds + allowance_remaining

    This includes the user's remaining free allowance so that UI elements (e.g.
    a "Send" button) correctly reflect what ``reserve()`` will actually admit.
    It is therefore **different** from ``AvailableResult.available`` (returned
    by ``get_available()``) which is cash-only: ``balance − active_holds``.
    """

    affordable: bool = False
    spendable: Decimal = Decimal(0)
    worst_case: Decimal = Decimal(0)
    reason: str | None = None


class AvailableResult(BaseModel):
    """Advisory available-balance read: ``available = balance − reserved`` (D4/H3).

    ``available`` is **cash-only** — it does not include free allowance.
    Use ``can_afford()`` (which returns ``CanAffordResult``) when you need
    the effective spending power including allowance headroom.
    """

    user_id: str
    balance: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    available: Decimal = Decimal(0)


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
    tier_breakdown: dict[str, Decimal] | None = None


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


class OperationPolicy(BaseModel):
    """Per-operation financial-safety policy (interface plan §1).

    Resolved per call as: explicit arg → ``PlanDefinition.per_operation[type]`` →
    plan default → the manager's constructor preset. ``max_concurrent`` bounds the
    number of simultaneously-active leases for an operation type; ``overdraft_floor``
    (only meaningful when ``billing_mode == "overdraft"``) is the negative balance
    floor admission is allowed down to.
    """

    billing_mode: BillingMode = "strict"
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None


class PlanDefinition(BaseModel):
    """Definition of a subscription plan with free allowance and rate overrides.

    Beyond allowance/rates/features, a plan carries the **financial-safety policy**
    (interface plan §1): a ``default_billing_mode`` for the whole plan, optional
    ``per_operation`` overrides keyed by operation type, and plan-wide
    ``max_concurrent`` / ``overdraft_floor`` defaults.

    Accepts ``billing_mode`` as a short-form alias for ``default_billing_mode``
    so configs written with the shorter key work without changes.
    """

    id: str
    name: str
    free_allowance: Decimal = Field(default=Decimal(0), ge=0)
    rate_overrides: dict[str, str] | None = None
    features: dict[str, Any] | None = None
    default_billing_mode: BillingMode = "strict"
    per_operation: dict[str, OperationPolicy] | None = None
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    #: Free-allowance reset window (WS9). ``calendar_month`` (default) resets on
    #: the 1st of each UTC month; ``rolling_30d``/``anniversary`` anchor to when
    #: the user was assigned the plan (``GetUserPlanResult.plan_assigned_at``).
    allowance_period: Literal["calendar_month", "rolling_30d", "anniversary"] = "calendar_month"

    @model_validator(mode="before")
    @classmethod
    def _normalise_billing_mode_alias(cls, data: Any) -> Any:
        """Accept ``billing_mode`` as an alias for ``default_billing_mode``."""
        if isinstance(data, dict) and "billing_mode" in data and "default_billing_mode" not in data:
            data = dict(data)
            data["default_billing_mode"] = data.pop("billing_mode")
        return data


class TierDefinition(BaseModel):
    """Definition of a credit tier controlling deduction priority and expiry.

    ``priority`` controls deduction order (lower drains first; ties broken by
    key ascending, not an error). ``expires`` marks whether credits granted
    into this tier are subject to expiry; when ``True``, ``default_ttl_days``
    (if set) fixes the default expiry window used by ``add_credits`` calls
    that omit an explicit ``expires_at``. At most one tier may set
    ``allow_overdraft=True`` (the sole tier permitted to absorb overdraft
    debt) and at most one may set ``is_default=True`` (untagged
    ``add_credits()`` calls land here) — enforced by config validation.
    """

    name: str
    priority: int
    expires: bool = False
    default_ttl_days: int | None = None
    allow_overdraft: bool = False
    is_default: bool = False


class AllowanceResult(BaseModel):
    """Result of checking plan allowance."""

    plan_id: str
    allowance_remaining: Decimal
    period_start: str
    period_end: str


class GetUserPlanResult(BaseModel):
    """Result of fetching a user's current plan.

    Carries the plan's financial-safety policy (``default_billing_mode``,
    ``per_operation``, ``max_concurrent``, ``overdraft_floor``) so the manager
    can resolve admission policy without a second round-trip (interface plan §1).

    ``allowance_period`` and ``plan_assigned_at`` (WS9) let the manager resolve
    the user's allowance reset window in one call, without a second round-trip
    to the store.
    """

    user_id: str
    plan_id: str | None = None
    plan_name: str | None = None
    free_allowance: Decimal = Decimal(0)
    features: dict[str, Any] = Field(default_factory=dict)
    default_billing_mode: BillingMode = "strict"
    per_operation: dict[str, OperationPolicy] = Field(default_factory=dict)
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    allowance_period: Literal["calendar_month", "rolling_30d", "anniversary"] = "calendar_month"
    plan_assigned_at: datetime | None = None


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
    tier_breakdown: dict[str, Decimal] | None = None


class SweepResult(BaseModel):
    """Result of sweeping expired credits."""

    expired_count: int = 0
    expired_amount: Decimal = Decimal(0)
    dry_run: bool = False
    expired_by_tier: dict[str, Decimal] | None = None


# ── Tier query API ──────────────────────────────────────────────────────


class TierBalance(BaseModel):
    """Balance for a single credit tier."""

    tier_key: str
    name: str = ""
    priority: int = 0
    expires: bool = False
    balance: Decimal = Decimal(0)


class TierBalancesResult(BaseModel):
    """Per-tier balance breakdown for a user, ordered by priority ascending.

    ``total_balance`` mirrors ``BalanceResult.balance`` — the maintained
    aggregate cache, kept equal to the sum of tier balances.
    """

    user_id: str
    tiers: list[TierBalance]
    total_balance: Decimal


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
