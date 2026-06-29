"""Abstract credit store interface.

All credit operations happen through a ``CreditStore`` adapter. This lets
the package work with Supabase (via RPCs), vanilla PostgreSQL, or in-memory
stores for testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from ducto.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    BalanceResult,
    CapCheckResult,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PricingConfigData,
    PricingConfigHistoryItem,
    PricingConfigResult,
    RefundResult,
    ReserveResult,
    SetupResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TopUserRow,
    TransactionRow,
)


class StoreError(Exception):
    """Base exception for store-level errors (connection, timeout, etc.)."""


class CapReachedError(StoreError):
    """Raised when a deduction would exceed a configured ``deny`` spend cap.

    Stores return ``error="cap_reached"`` on the result model rather than
    raising; the manager maps that code to this exception (contract §4).
    """


class RefundError(StoreError):
    """Raised when a refund is invalid (over-refund, duplicate, wrong type).

    Stores return a business code (``already_refunded``/``over_refund``/
    ``not_found``) on ``RefundResult.error``; the manager maps it to this
    exception (contract §4).
    """


class CreditStore(ABC):
    """Interface for credit storage backends.

    Implementors provide concrete adapters for Supabase, raw PostgreSQL,
    or in-memory stores.
    """

    # ── Schema management ──────────────────────────────────────────────

    @abstractmethod
    def setup(self, database_url: str | None = None) -> SetupResult:
        """Run bundled SQL migrations (tables, indexes, RPCs).

        Idempotent — safe to call on every deploy.

        Args:
            database_url: Postgres connection string. Required for stores
                that manage schema setup directly (``HttpxSupabaseStore``,
                ``PostgresStore``). Ignored by in-memory stores.
        """
        ...

    # ── Runtime operations ─────────────────────────────────────────────

    @abstractmethod
    def get_balance(self, user_id: str) -> BalanceResult:
        """Return current balance and lifetime purchased amount."""
        ...

    @abstractmethod
    def add_credits(
        self,
        user_id: str,
        amount: Decimal,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        """Atomically add credits and log a transaction.

        Args:
            amount: Fractional credit amount (``Decimal``).
            expires_at: Optional datetime after which the credits expire.
        """
        ...

    @abstractmethod
    def deduct_with_allowance(
        self,
        user_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        """Atomically charge a gross cost in a single server-side transaction.

        This is the canonical "calculate cost then charge now" path (contract
        §2). Within one transaction the store:

        1. Locks the user's credit row.
        2. Honors ``idempotency_key`` (user-scoped) — a replay returns the
           original result with ``idempotent=True``.
        3. Consumes free allowance first (``allowance_consumed`` on the result),
           charging only the net remainder to the balance.
        4. Enforces spend caps on the net: a ``deny`` cap aborts with
           ``error="cap_reached"`` (no allowance consumed); ``warn``/``notify``
           set ``cap_warning`` and continue.
        5. Enforces the balance floor: ``balance - net < min_balance`` aborts
           with ``error="insufficient_credits"`` (no allowance consumed).
        6. Debits the balance and inserts one ``usage`` transaction.

        All-or-nothing: any failure rolls back allowance consumption and the
        balance change. Business failures are returned via
        ``DeductionResult.error`` (the manager maps codes to exceptions); the
        store does not import manager-level exceptions.

        Args:
            user_id: The user to charge.
            amount: Gross cost (``Decimal``, ``>= 0``, fractional 4dp).
            idempotency_key: Optional user-scoped replay key.
            min_balance: Minimum balance floor (default ``Decimal(0)``).
            model: Optional model name recorded on the transaction.
            metadata: Extra metadata merged onto the transaction.

        Returns:
            ``DeductionResult`` with net ``amount``, ``allowance_consumed``,
            ``balance_after``, ``idempotent``, ``cap_warning``, and ``error``.
        """
        ...

    @abstractmethod
    def reserve_credits(
        self,
        user_id: str,
        amount: Decimal,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: Decimal = Decimal(5),
    ) -> ReserveResult:
        """Reserve credits for an upcoming operation.

        Locks the user row to prevent concurrent overspend.
        Returns a ``ReserveResult`` with ``error`` set on failure.
        """
        ...

    @abstractmethod
    def deduct_credits(
        self,
        user_id: str,
        reservation_id: str,
        amount: Decimal,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        """Finalize a credit deduction and release the reservation.

        If ``idempotency_key`` is provided and a matching transaction already
        exists, returns the existing result (idempotent replay).
        """
        ...

    # ── Pricing configuration ──────────────────────────────────────────

    @abstractmethod
    def get_active_pricing(self) -> PricingConfigResult | None:
        """Fetch the active pricing configuration from the store."""
        ...

    @abstractmethod
    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        """Publish a new pricing configuration.

        Deactivates the previous active config and inserts a new one.
        Returns the new config id.
        """
        ...

    @abstractmethod
    def get_pricing_history(self) -> list[PricingConfigHistoryItem]:
        """List all pricing config versions (newest first)."""
        ...

    @abstractmethod
    def get_pricing_config(self, version: int) -> PricingConfigResult | None:
        """Fetch a specific pricing config by version number."""
        ...

    @abstractmethod
    def activate_pricing(self, version: int) -> str:
        """Activate a specific pricing version (deactivates all others).

        Args:
            version: The version number to activate.

        Returns:
            The activated config id.
        """
        ...

    # ── Plan management ────────────────────────────────────────────────

    @abstractmethod
    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        """Fetch user's current plan (including feature entitlements)."""
        ...

    def check_feature(self, user_id: str, feature: str) -> CheckFeatureResult:
        """Check whether a user's plan has a specific feature entitlement.

        Convenience method. Default implementation calls ``get_user_plan()``
        and inspects the ``features`` dict. Override in custom stores for
        optimized queries.

        Feature presence is distinguished from truthiness (contract §5, M6):
        the feature is considered present when the key exists and its value is
        not ``None``/``False``. Numeric ``0`` and empty string ``""`` are
        therefore *present* (``has_feature=True``).
        - absent / ``None`` / ``False`` → ``has_feature=False``
        - ``True`` / numeric (incl. ``0``) / string (incl. ``""``) → ``has_feature=True``

        Note: identity checks (``is None``/``is False``) are used rather than the
        contract's literal ``not in (None, False)``, because ``0 == False`` /
        ``0.0 == False`` in Python would otherwise mis-classify numeric ``0`` as
        absent — defeating the very M6 intent ("numeric ``0``/``""`` ⇒ present").
        """
        plan = self.get_user_plan(user_id)
        value = plan.features.get(feature)
        has_feature = feature in plan.features and value is not None and value is not False
        return CheckFeatureResult(
            user_id=user_id,
            feature=feature,
            value=value,
            has_feature=has_feature,
        )

    @abstractmethod
    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        """Assign a plan to a user."""
        ...

    @abstractmethod
    def check_allowance(self, user_id: str) -> AllowanceResult:
        """Get remaining free allowance for current billing period."""
        ...

    @abstractmethod
    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        """Record allowance consumption for current billing period."""
        ...

    # ── Spend caps and rate limiting ────────────────────────────────────

    @abstractmethod
    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: Decimal | None = None,
    ) -> CapCheckResult:
        """Check whether a pending deduction would exceed any configured cap.

        Args:
            user_id: The user to check caps for.
            model: Optional model name for per-model caps.
            amount: The pending deduction amount.

        Returns:
            ``CapCheckResult`` with the check result.
        """
        ...

    # ── Refunds ─────────────────────────────────────────────────────────

    @abstractmethod
    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        """Refund a previous credit deduction.

        Args:
            transaction_id: The transaction to refund.
            amount: Optional partial refund amount. Full refund if omitted.
            reason: Optional reason for the refund.
            metadata: Extra metadata to attach to the refund transaction.

        Returns:
            ``RefundResult`` with the refund transaction details, or
            ``error`` set if the transaction doesn't exist or is already refunded.
        """
        ...

    # ── Credit expiry ───────────────────────────────────────────────────

    @abstractmethod
    def sweep_expired_credits(
        self,
        dry_run: bool = False,
    ) -> SweepResult:
        """Sweep expired credits from all users' balances.

        Args:
            dry_run: If True, report what would be expired without modifying.

        Returns:
            ``SweepResult`` with count and amount of expired credits.
        """
        ...

    # ── Usage analytics ─────────────────────────────────────────────────

    @abstractmethod
    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Aggregate spend by user in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``SpendByUserRow`` with totals per user.
        """
        ...

    @abstractmethod
    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Aggregate spend by model in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``SpendByModelRow`` with totals per model.
        """
        ...

    @abstractmethod
    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        """Top users by spend in a time window.

        Args:
            limit: Maximum number of users to return.
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``TopUserRow`` sorted by total_spend descending.
        """
        ...

    @abstractmethod
    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        """Daily spend aggregation in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``DailySpendRow`` with per-day totals.
        """
        ...

    @abstractmethod
    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        """Aggregate statistics across all users in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            ``AggregateStatsRow`` with total credits consumed, active users,
            average daily spend, top model, and top user.
        """
        ...

    # ── Transaction listing ─────────────────────────────────────────────────

    @abstractmethod
    def list_user_transactions(
        self,
        user_id: str,
        types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TransactionRow]:
        """List credit transactions for a user with pagination.

        Args:
            user_id: The user to query.
            types: Optional filter by transaction types (e.g. ["usage"]).
            from_date: Optional start of date range (inclusive).
            to_date: Optional end of date range (inclusive).
            limit: Maximum rows to return (default 50).
            offset: Number of rows to skip (default 0).

        Returns:
            List of ``TransactionRow`` objects. Each row includes ``total_count``
            representing the total matching rows before pagination.
        """
        ...

    # ── Team/shared balance pools ─────────────────────────────────────────

    @abstractmethod
    def create_team(
        self,
        name: str,
        initial_balance: Decimal = Decimal(0),
    ) -> CreateTeamResult:
        """Create a team with a shared credit balance pool.

        Args:
            name: Human-readable team name.
            initial_balance: Starting credit balance.

        Returns:
            ``CreateTeamResult`` with the new team id.
        """
        ...

    @abstractmethod
    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        """Fetch team balance and member count.

        Args:
            team_id: The team's UUID.

        Returns:
            ``TeamBalanceResult`` with balance and member count.
        """
        ...

    @abstractmethod
    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: Decimal | None = None,
    ) -> AddTeamMemberResult:
        """Add a user to a team.

        Args:
            team_id: The team's UUID.
            user_id: The user's UUID.
            role: Member role (e.g. "member", "admin").
            spend_cap: Optional per-user spend cap.

        Returns:
            ``AddTeamMemberResult`` confirming membership.
        """
        ...

    @abstractmethod
    def get_team_members(self, team_id: str) -> list[TeamMember]:
        """List all members of a team.

        Args:
            team_id: The team's UUID.

        Returns:
            List of ``TeamMember``.
        """
        ...

    @abstractmethod
    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: Decimal,
        metadata: CreditMetadata | None = None,
        idempotency_key: str | None = None,
    ) -> TeamDeductionResult:
        """Deduct credits from a team pool, attributed to a user.

        Args:
            team_id: The team's UUID.
            user_id: The user to attribute the deduction to.
            amount: Credits to deduct (``Decimal``).
            metadata: Extra metadata.
            idempotency_key: Optional replay key. A retried team deduction with
                the same key returns the original result rather than charging
                the shared pool again (contract §2/H12).

        Returns:
            ``TeamDeductionResult`` with transaction details.
        """
        ...
