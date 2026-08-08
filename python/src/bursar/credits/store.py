"""Abstract credit store interface.

All credit operations happen through a ``CreditStore`` adapter. The
production adapter is ``PostgresStore``; custom stores implement this
interface to back Bursar with their own storage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bursar.config import CatalogRollout
from bursar.credits.types import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStats,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BillingMode,
    BucketBalancesResult,
    CatalogRevision,
    CatalogRevisionSummary,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    ExecuteGrantProgramRequest,
    GetUserPlanResult,
    GrantProgramAwardResult,
    LeasePricingContext,
    LeaseResult,
    LedgerCursor,
    LedgerEntry,
    LedgerPage,
    ListQuotaEventsOptions,
    PlanMigrationBatchResult,
    PlanMigrationStartResult,
    QuotaEvent,
    QuotaState,
    RefundResult,
    ReleaseResult,
    RevokeCreditsResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TeamRole,
    TopUserRow,
    UnsetUserPlanResult,
    UsageChargeCursor,
    UsageChargePage,
    UsageRecordResult,
)
from bursar.errors import (
    CapabilityNotSupportedError,
)
from bursar.errors import (
    CapReachedError as CapReachedError,
)
from bursar.errors import (
    RefundError as RefundError,
)
from bursar.errors import (
    StoreClosedError as StoreClosedError,
)
from bursar.errors import (
    StoreError as StoreError,
)
from bursar.errors import (
    StoreTimeoutError as StoreTimeoutError,
)
from bursar.errors import (
    StoreUnavailableError as StoreUnavailableError,
)


class _CreditStoreOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationUsageOptions(_CreditStoreOptions):
    feature: str | None = None
    model: str | None = None
    region: str | None = None
    measures: dict[str, Any] | None = None
    dimensions: dict[str, Any] | None = None


class CreateLeaseOptions(OperationUsageOptions):
    idempotency_key: str | None = None
    billing_mode: BillingMode = "strict"
    floor: Decimal = Decimal(0)
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    ttl_seconds: int = Field(default=600, ge=1)
    metadata: CreditMetadata | None = None


class SettleLeaseOptions(OperationUsageOptions):
    idempotency_key: str | None = None
    metadata: CreditMetadata | None = None


class CreditStore(ABC):
    """Interface for credit storage backends.

    ``PostgresStore`` is the production adapter. Custom stores implement this
    interface to back Bursar with their own storage.
    """

    def __init__(self) -> None:
        """Initialize the store."""
        super().__init__()

    def close(self) -> None:
        """Release resources owned by the store.

        Stateless/custom stores need no cleanup. Connection-backed stores should
        override this method so applications can close them through the Bursar
        facade instead of retaining an adapter-specific reference.
        """
        return

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
        bucket: str | None = None,
        idempotency_key: str | None = None,
    ) -> AddCreditsResult:
        """Atomically add credits and log a transaction.

        Args:
            amount: Fractional credit amount (``Decimal``).
            expires_at: Optional datetime after which the credits expire.
            bucket: Optional bucket key to grant into. When no buckets are
                configured, must be ``None`` or ``"default"``. When buckets are
                configured and omitted, resolves to the bucket with
                ``is_default=True`` (raises if none is marked default).
            idempotency_key: Optional user-scoped replay key. A retried grant
                with the same key (e.g. a webhook redelivered by the sender)
                returns the original entry's result rather than
                granting a second time — no double-mutation, no second
                ledger row. Follows the same replay idiom already used by
                :meth:`deduct_with_allowance`/:meth:`settle_lease`/
                :meth:`deduct_team`.
        """
        ...

    @abstractmethod
    def deduct_with_allowance(
        self,
        user_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        operation: str = "usage",
        feature: str | None = None,
        model: str | None = None,
        region: str | None = None,
        measures: dict[str, Decimal | int | float] | None = None,
        dimensions: dict[str, Any] | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        """Atomically charge a gross cost in a single server-side transaction.

        This is the canonical "calculate cost then charge now" path (contract
        §2). Within one transaction the store:

        1. Locks the user's credit row.
        2. Honors ``idempotency_key`` (user-scoped) — a replay returns the
            original result with ``idempotent=True``. The replayed
            ``balance_after`` is the balance at the time of the *original* call,
            not the current balance.
        3. Consumes free allowance first (``allowance_consumed`` on the result),
            charging only the net remainder to the balance.
        4. Enforces the canonical plan credit policy and quotas server-side.
        5. Debits the balance and inserts one ``usage`` transaction.

        All-or-nothing: any failure rolls back allowance consumption and the
        balance change. Business failures are returned via
        ``DeductionResult.error`` (the manager maps codes to exceptions); the
        store does not import manager-level exceptions.

        Args:
            user_id: The user to charge.
            amount: Gross cost (``Decimal``, ``>= 0``, fractional 6dp).
            idempotency_key: Optional user-scoped replay key.
            operation: Catalog operation key used for plan-aware policy.
            feature: Optional feature key used for entitlement checks.
            model: Optional model name recorded on the transaction.
            region: Optional deployment region recorded on the transaction.
            measures: Usage measures evaluated by quota rules.
            dimensions: Usage dimensions evaluated by pricing/policy rules.
            metadata: Extra metadata merged onto the transaction.

        Returns:
            ``DeductionResult`` with net ``amount``, ``allowance_consumed``,
            ``balance_after``, ``idempotent``, and ``error``.
        """
        ...

    # ── Lease lifecycle (atomic admission) ─────────────────────────────
    #
    # The lease is the canonical admission primitive.
    # ``reserve``/``settle``/``release``/``renew`` on the manager map onto these.
    # Leases reuse the credit_reservations table/records extended with a status
    # (active → settled | released | expired), a billing mode, and an overdraft
    # floor. ``available = balance − Σ(amount WHERE status='active' AND unexpired)``.

    @abstractmethod
    def create_lease(
        self,
        user_id: str,
        amount: Decimal,
        operation_type: str,
        options: CreateLeaseOptions | None = None,
    ) -> LeaseResult:
        """Atomically acquire a lease (hold) — the only authoritative admission control.

        Under one lock the store: (1) ensures the balance row exists; (2) enforces
        ``max_concurrent`` by **counting active leases** for ``(user_id,
        operation_type)``; (3) enforces canonical entitlements and quotas;
        (4) computes ``available = balance − Σ active holds`` and rejects with
        ``error="insufficient_credits"`` if ``available − amount < floor``; (5)
        inserts an ``active`` lease expiring after ``ttl_seconds``.

        ``floor`` is the resolved admission floor (``>= 0`` for strict; the negative
        ``overdraft_floor`` for overdraft). ``billing_mode``/``overdraft_floor`` are
        persisted on the lease for settle-time/observability. Business failures are
        returned via ``LeaseResult.error``; the store never raises domain exceptions.
        """
        ...

    @abstractmethod
    def settle_lease(
        self,
        user_id: str,
        lease_id: str,
        amount: Decimal,
        options: SettleLeaseOptions | None = None,
    ) -> DeductionResult:
        """Charge the actual cost against a lease, then mark it settled.

        De-clamped: charges ``amount`` even if it exceeds the lease hold (overdraft),
        never clamps to the lease amount.
        Pipeline: idempotency replay → allowance consumption → quota accounting →
        debit (the balance may go negative in overdraft) → ledger row → mark the
        lease ``settled``. ``amount == 0`` releases the lease without charging.

        Lease-state failures are returned via ``DeductionResult.error``:
        ``lease_not_found`` (missing / other user / released) or ``lease_expired``
        (the lease TTL elapsed). A replayed settle
        (same idempotency key, or a re-settle of an already-settled lease) returns
        the original result with ``idempotent=True``.
        """
        ...

    @abstractmethod
    def get_lease_pricing_context(self, user_id: str, lease_id: str) -> LeasePricingContext | None:
        """Return the catalog revision and rate card captured by a lease.

        Usage-metric settlement must price against this immutable context rather
        than the subject's current plan, which may have changed after admission.
        ``None`` means the lease is missing or does not belong to ``user_id``.
        """
        ...

    @abstractmethod
    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        """Release a lease without charging (work failed/aborted).

        Idempotent and safe on missing or already-finalized leases:
        transitions an ``active``/``expired`` lease to ``released`` and reports
        ``released=True``; otherwise reports ``released=False`` with a ``reason``.
        """
        ...

    @abstractmethod
    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        """Extend an active lease without changing its captured policy."""
        ...

    def expire_leases(self, limit: int = 100) -> int:
        """Expire a bounded batch of abandoned leases and release reservations."""
        raise CapabilityNotSupportedError("expire_leases is not supported by this store")

    @abstractmethod
    def get_available(self, user_id: str) -> AvailableResult:
        """Advisory, non-locking read of ``available = balance − Σ active holds``.

        For UI only — never an admission gate; the value may be stale the
        instant it is read.
        """
        ...

    @abstractmethod
    def get_bucket_balances(self, user_id: str) -> BucketBalancesResult:
        """Return per-bucket balance breakdown for a user, ordered by priority ascending.

        When no buckets are configured, returns a single synthetic ``"default"``
        bucket entry so the shape is uniform regardless of whether buckets are
        configured.
        """
        ...

    def execute_grant_program(
        self,
        request: ExecuteGrantProgramRequest,
    ) -> list[GrantProgramAwardResult]:
        """Execute one configured grant-program event."""
        raise CapabilityNotSupportedError("execute_grant_program is not supported by this store")

    # ── Catalog configuration ──────────────────────────────────────────

    @abstractmethod
    def get_active_catalog(self) -> CatalogRevision | None:
        """Fetch the active catalog revision from the store."""
        ...

    @abstractmethod
    def publish_and_activate_catalog(
        self,
        config: dict[str, Any],
        label: str | None = None,
        rollout: CatalogRollout | dict[str, Any] | None = None,
    ) -> str:
        """Publish and activate a catalog revision.

        Deactivates the previous active config and inserts a new one.
        Returns the new config id.
        """
        ...

    @abstractmethod
    def get_catalog_history(self) -> list[CatalogRevisionSummary]:
        """List catalog revisions, newest first."""
        ...

    @abstractmethod
    def get_catalog_revision(self, version: int) -> CatalogRevision | None:
        """Fetch a catalog revision by version number."""
        ...

    @abstractmethod
    def activate_catalog_revision(
        self,
        version: int,
        rollout: CatalogRollout | dict[str, Any] | None = None,
    ) -> str:
        """Activate a catalog revision (deactivates all others).

        Args:
            version: The version number to activate.

        Returns:
            The activated config id.
        """
        ...

    @abstractmethod
    def publish_catalog_draft(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        """Publish an inactive catalog draft without changing the live catalog."""
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

        Feature presence is distinguished from truthiness:
        the feature is considered present when the key exists and its value is
        not ``None``/``False``. Numeric ``0`` and empty string ``""`` are
        therefore *present* (``has_feature=True``).
        - absent / ``None`` / ``False`` → ``has_feature=False``
        - ``True`` / numeric (incl. ``0``) / string (incl. ``""``) → ``has_feature=True``

        Note: identity checks (``is None``/``is False``) are used rather than the
        contract's literal ``not in (None, False)``, because ``0 == False`` /
        ``0.0 == False`` in Python would otherwise mis-classify numeric ``0`` as
        absent even though numeric ``0`` and ``""`` are present values.
        """
        plan = self.get_user_plan(user_id)
        entitlement = plan.entitlements.get(feature)
        value = entitlement.value if entitlement else None
        has_feature = feature in plan.entitlements and value is not None and value is not False
        return CheckFeatureResult(
            user_id=user_id,
            feature=feature,
            value=value,
            has_feature=has_feature,
        )

    @abstractmethod
    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        """Assign a plan to a user.

        ``plan_assigned_at`` anchors plan-assignment policy windows. When
        omitted, the store uses the current time.
        """
        ...

    @abstractmethod
    def unset_user_plan(self, user_id: str) -> UnsetUserPlanResult:
        """Clear the user's plan assignment."""
        ...

    @abstractmethod
    def set_plan_revision_pin(self, user_id: str, pinned: bool) -> bool:
        """Pin or unpin the current assignment's catalog revision."""
        ...

    @abstractmethod
    def apply_due_plan_changes(self, limit: int = 100) -> int:
        """Apply a bounded batch of scheduled plan changes that are now due."""
        ...

    @abstractmethod
    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> PlanMigrationStartResult:
        """Create a resumable migration from one catalog plan to another."""
        ...

    @abstractmethod
    def migrate_plan_batch(
        self,
        migration_id: str,
        batch_size: int = 100,
    ) -> PlanMigrationBatchResult:
        """Advance a plan migration by one bounded batch."""
        ...

    @abstractmethod
    def get_quota_state(
        self,
        user_id: str,
        quota_key: str | None = None,
    ) -> list[QuotaState]:
        """Return current quota windows for a user."""
        ...

    @abstractmethod
    def check_allowance(self, user_id: str) -> AllowanceResult | None:
        """Get the database-owned current allowance window."""
        ...

    @abstractmethod
    def list_quota_events(
        self,
        user_id: str,
        options: ListQuotaEventsOptions | None = None,
    ) -> list[QuotaEvent]:
        """List persisted quota threshold and blocking events."""
        ...

    # ── Refunds ─────────────────────────────────────────────────────────

    @abstractmethod
    def refund_credits(
        self,
        entry_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
        idempotency_key: str | None = None,
    ) -> RefundResult:
        """Refund a previous credit deduction.

        Args:
            entry_id: The transaction to refund.
            amount: Optional partial refund amount. Full refund if omitted.
            reason: Optional reason for the refund.
            metadata: Extra metadata to attach to the refund entry.
            idempotency_key: Stable replay key. A deterministic key is generated
                from ``entry_id`` and ``amount`` when omitted.

        Returns:
            ``RefundResult`` with the refund ledger entry details, or
            ``error`` set if the transaction doesn't exist or is already refunded.
        """
        ...

    # ── Credit expiry ───────────────────────────────────────────────────

    @abstractmethod
    def sweep_expired_credits(
        self,
        dry_run: bool = False,
        user_id: str | None = None,
        limit: int = 100,
    ) -> SweepResult:
        """Expire at most ``limit`` eligible credit lots."""
        ...

    @abstractmethod
    def revoke_credits_by_entry_type(
        self,
        user_id: str,
        entry_type: str,
    ) -> RevokeCreditsResult:
        """Revoke all credits of a given transaction type for a user (LIFO across tiers).

        Used by the subscription lifecycle to replace cycle-grant credits on renewal.
        Returns the revoked amount and resulting committed balance.
        """
        ...

    # ── Usage analytics (optional capability) ────────────────────────────
    #
    # These methods have a default implementation that raises
    # CapabilityNotSupportedError. Override them to support usage analytics;
    # a minimal custom store does not need to implement this group at all.

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Aggregate spend by user in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``SpendByUserRow`` with totals per user.
        """
        raise CapabilityNotSupportedError("spend_by_user is not supported by this store")

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Aggregate spend by model in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``SpendByModelRow`` with totals per model.
        """
        raise CapabilityNotSupportedError("spend_by_model is not supported by this store")

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        """Top users by spend in a time window.

        Args:
            limit: Maximum number of users to return.
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``TopUserRow`` sorted by total_spend descending.
        """
        raise CapabilityNotSupportedError("top_users is not supported by this store")

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        """Daily spend aggregation in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            List of ``DailySpendRow`` with per-day totals.
        """
        raise CapabilityNotSupportedError("daily_spend is not supported by this store")

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats:
        """Aggregate statistics across all users in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            ``AggregateStats`` with total credits consumed, active users,
            average daily spend, top model, and top user.
        """
        raise CapabilityNotSupportedError("aggregate_stats is not supported by this store")

    # ── Canonical ledger history ───────────────────────────────────────

    def list_ledger_entries(
        self,
        user_id: str,
        entry_types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage:
        """List account ledger history with a stable timestamp-plus-entry cursor."""
        raise CapabilityNotSupportedError("list_ledger_entries not supported by this store")

    def list_usage_entries(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage:
        """List usage ledger entries with the same cursor contract."""
        return self.list_ledger_entries(user_id, ["usage"], from_date, to_date, limit, cursor)

    def list_usage_charges(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: UsageChargeCursor | None = None,
        include_record_only: bool = True,
    ) -> UsageChargePage:
        """List metered usage charges, including allowance-covered events."""
        raise CapabilityNotSupportedError("list_usage_charges not supported by this store")

    def record_usage(
        self,
        user_id: str,
        operation: str,
        requested: Decimal,
        *,
        idempotency_key: str,
        feature: str | None = None,
        model: str | None = None,
        region: str | None = None,
        metadata: CreditMetadata | None = None,
        measures: dict[str, Any] | None = None,
        dimensions: dict[str, Any] | None = None,
    ) -> UsageRecordResult:
        """Append priced usage telemetry without debiting the account again."""
        raise CapabilityNotSupportedError("record_usage not supported by this store")

    def get_ledger_entry(self, user_id: str, entry_id: str) -> LedgerEntry | None:
        """Return one ledger entry when it belongs to the user account."""
        raise CapabilityNotSupportedError("get_ledger_entry not supported by this store")

    # ── Team/shared balance pools (optional capability) ───────────────────

    def create_team(
        self,
        owner_subject_id: str,
        name: str,
        initial_balance: Decimal = Decimal(0),
    ) -> CreateTeamResult:
        """Create a team with a shared credit balance pool.

        Args:
            owner_subject_id: Subject that owns the team.
            name: Human-readable team name.
            initial_balance: Starting credit balance.

        Returns:
            ``CreateTeamResult`` with the new team id.
        """
        raise CapabilityNotSupportedError("create_team is not supported by this store")

    def get_team_balance(self, team_id: str) -> TeamBalanceResult | None:
        """Fetch team balance and member count.

        Args:
            team_id: The team's UUID.

        Returns:
            ``TeamBalanceResult`` with balance and member count, or ``None``
            when the team does not exist.
        """
        raise CapabilityNotSupportedError("get_team_balance is not supported by this store")

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: TeamRole = "member",
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
        raise CapabilityNotSupportedError("add_team_member is not supported by this store")

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        """List all members of a team.

        Args:
            team_id: The team's UUID.

        Returns:
            List of ``TeamMember``.
        """
        raise CapabilityNotSupportedError("get_team_members is not supported by this store")

    def remove_team_member(self, team_id: str, user_id: str) -> bool:
        """Remove a team member unless they are the final owner."""
        raise CapabilityNotSupportedError("remove_team_member is not supported by this store")

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
                the shared pool again.

        Returns:
            ``TeamDeductionResult`` with ledger entry details.
        """
        raise CapabilityNotSupportedError("deduct_team is not supported by this store")
