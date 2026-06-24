"""Abstract credit store interface.

All credit operations happen through a ``CreditStore`` adapter. This lets
the package work with Supabase (via RPCs), vanilla PostgreSQL, or in-memory
stores for testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ducto.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AllowanceResult,
    BalanceResult,
    CapCheckResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PricingConfigData,
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
)


class StoreError(Exception):
    """Base exception for store-level errors (connection, timeout, etc.)."""


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
        amount: int,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        """Atomically add credits and log a transaction.

        Args:
            expires_at: Optional datetime after which the credits expire.
        """
        ...

    @abstractmethod
    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: int = 5,
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
        amount: int,
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

    # ── Plan management ────────────────────────────────────────────────

    @abstractmethod
    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        """Fetch user's current plan (if any)."""
        ...

    @abstractmethod
    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        """Assign a plan to a user."""
        ...

    @abstractmethod
    def check_allowance(self, user_id: str) -> AllowanceResult:
        """Get remaining free allowance for current billing period."""
        ...

    @abstractmethod
    def increment_usage_window(self, user_id: str, plan_id: str, amount: int) -> None:
        """Record allowance consumption for current billing period."""
        ...

    # ── Spend caps and rate limiting ────────────────────────────────────

    @abstractmethod
    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: int | None = None,
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
        amount: int | None = None,
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

    # ── Team/shared balance pools ─────────────────────────────────────────

    @abstractmethod
    def create_team(
        self,
        name: str,
        initial_balance: int = 0,
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
        spend_cap: int | None = None,
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
        amount: int,
        metadata: CreditMetadata | None = None,
    ) -> TeamDeductionResult:
        """Deduct credits from a team pool, attributed to a user.

        Args:
            team_id: The team's UUID.
            user_id: The user to attribute the deduction to.
            amount: Credits to deduct.
            metadata: Extra metadata.

        Returns:
            ``TeamDeductionResult`` with transaction details.
        """
        ...
