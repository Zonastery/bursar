"""Abstract credit store interface.

All credit operations happen through a ``CreditStore`` adapter. This lets
the package work with Supabase (via RPCs), vanilla PostgreSQL, or in-memory
stores for testing.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from bursar.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    CapCheckResult,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    FeatureLimit,
    FeatureLimitResult,
    GetUserPlanResult,
    LeaseResult,
    PricingConfigData,
    PricingConfigHistoryItem,
    PricingConfigResult,
    RefundResult,
    ReleaseResult,
    SetupResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TierBalancesResult,
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


class FeatureLimitReachedError(StoreError):
    """Raised when a call would exceed a configured ``deny`` feature-limit.

    Stores return ``error="feature_limit_reached"`` on the result model rather
    than raising; the manager maps that code to this exception — mirrors
    ``CapReachedError``.
    """


class CapabilityNotSupportedError(StoreError):
    """Raised when a store does not implement an optional capability (WS8).

    The ``CreditStore`` ABC only requires core credit ops, lease lifecycle,
    pricing config versioning, plan management, spend caps, refunds, and
    expiry. Analytics, transaction-listing, and teams are optional
    capabilities with a default implementation that raises this error, so a
    minimal custom store subclass does not need to implement ~35 methods.
    """


class CreditStore(ABC):
    """Interface for credit storage backends.

    Implementors provide concrete adapters for Supabase, raw PostgreSQL,
    or in-memory stores.
    """

    def __init__(self, pricing_cache_ttl: int = 300) -> None:
        """Initialize the store.

        Args:
            pricing_cache_ttl: Seconds to cache ``get_active_pricing()``
                results. Set to ``0`` to disable caching. Default 300.
        """
        self._pricing_cache_ttl = pricing_cache_ttl
        self._pricing_cache_result: PricingConfigResult | None = None
        self._pricing_cache_time: float = 0.0
        self._pricing_cache_lock = threading.Lock()

    # ── Pricing cache ──────────────────────────────────────────────────

    def _get_cached_pricing(
        self,
        loader: Callable[[], PricingConfigResult | None],
    ) -> PricingConfigResult | None:
        """Return cached pricing if within TTL, else call *loader* and cache.

        Thread-safe. ``None`` results are **not** cached — a missing config
        triggers a backend call on every invocation so that newly-published
        pricing is picked up immediately.
        """
        now = time.monotonic()
        with self._pricing_cache_lock:
            if self._pricing_cache_result is not None and (now - self._pricing_cache_time) < self._pricing_cache_ttl:
                return self._pricing_cache_result
        result = loader()
        now = time.monotonic()  # re-read after loader to avoid TTL erosion
        with self._pricing_cache_lock:
            self._pricing_cache_result = result
            self._pricing_cache_time = now
        return result

    def invalidate_pricing_cache(self) -> None:
        """Force the next ``get_active_pricing()`` call to reload from the backend."""
        with self._pricing_cache_lock:
            self._pricing_cache_result = None
            self._pricing_cache_time = 0.0

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
        tier: str | None = None,
        idempotency_key: str | None = None,
    ) -> AddCreditsResult:
        """Atomically add credits and log a transaction.

        Args:
            amount: Fractional credit amount (``Decimal``).
            expires_at: Optional datetime after which the credits expire.
            tier: Optional tier key to grant into. When no tiers are
                configured, must be ``None`` or ``"default"``. When tiers are
                configured and omitted, resolves to the tier with
                ``is_default=True`` (raises if none is marked default).
            idempotency_key: Optional user-scoped replay key. A retried grant
                with the same key (e.g. a webhook redelivered by the sender)
                returns the original transaction's result rather than
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
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
        skip_allowance: bool = False,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> DeductionResult:
        """Atomically charge a gross cost in a single server-side transaction.

        This is the canonical "calculate cost then charge now" path (contract
        §2). Within one transaction the store:

        1. Locks the user's credit row.
        2. Honors ``idempotency_key`` (user-scoped) — a replay returns the
           original result with ``idempotent=True``. The replayed
           ``balance_after`` is the balance at the time of the *original* call,
           not the current balance (Fix 8).
        3. Consumes free allowance first (``allowance_consumed`` on the result),
           charging only the net remainder to the balance. Skipped entirely when
           ``skip_allowance=True`` so that fixed-cost batch jobs (daily_report,
           batch_train, …) do **not** eat the user's inference allowance (Fix 7).
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
            skip_allowance: When ``True`` the free-allowance step is bypassed
                entirely; the full ``amount`` is charged to the balance.
                Use for fixed-cost batch jobs that should not consume inference
                allowance (Fix 7).
            period_start: Explicit allowance-window start date (WS9), used to key
                allowance consumption for ``rolling_30d``/``anniversary`` plans.
                ``None`` (the default) falls back to the current UTC calendar
                month, matching the pre-WS9 behavior exactly.
            feature: Optional feature name tagged onto the inserted transaction's
                ``metadata.feature`` and, when ``feature_limit`` is given, checked
                against it. ``None`` skips feature-limit enforcement/tagging
                entirely (the manager only passes this when the caller named a
                feature via ``deduct(feature=...)``).
            feature_limit: The resolved ``FeatureLimit`` for ``feature`` (looked
                up from the user's plan by the manager). ``None`` means no limit
                is configured for this feature — enforcement is skipped (the
                transaction is still tagged with ``feature`` when given).
            feature_period_start: The calendar-aligned window start for
                ``feature_limit.period``, resolved by the manager via
                :func:`bursar.allowance.resolve_calendar_window`.

        Enforcement (inserted between the spend-cap step and the balance floor):
        counting is **ledger-derived**, exactly like spend caps — no separate
        counter table. The store counts prior committed ``usage`` transactions
        with ``metadata.feature == feature`` in ``[feature_period_start,
        feature_period_start + period)``; if that count has already reached
        ``feature_limit.max_calls`` and ``feature_limit.action == "deny"``,
        aborts with ``error="feature_limit_reached"`` (no allowance consumed,
        no transaction inserted); ``warn``/``notify`` instead set
        ``feature_limit_warning`` and continue. The ``usage`` transaction this
        call inserts on success (tagged ``metadata.feature``) *is* the count
        increment for future calls — there is nothing else to update. This
        also means a later :meth:`refund_credits` of this transaction does
        **not** free up quota (the row, and therefore the count, is untouched
        by a refund), and a :meth:`release_lease` never counts at all (it
        never inserts a ``usage`` row) — both are intentional.

        Returns:
            ``DeductionResult`` with net ``amount``, ``allowance_consumed``,
            ``balance_after``, ``idempotent``, ``cap_warning``,
            ``feature_limit_warning``, and ``error``.
        """
        ...

    # ── Lease lifecycle (atomic admission) ─────────────────────────────
    #
    # The lease is the canonical admission primitive (interface plan §3/D4).
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
        *,
        billing_mode: str = "strict",
        floor: Decimal = Decimal(0),
        max_concurrent: int | None = None,
        ttl_seconds: int = 600,
        model: str | None = None,
        overdraft_floor: Decimal | None = None,
        metadata: CreditMetadata | None = None,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> LeaseResult:
        """Atomically acquire a lease (hold) — the only admission control (D4).

        Under one lock the store: (1) ensures the balance row exists; (2) enforces
        ``max_concurrent`` by **counting active leases** for ``(user_id,
        operation_type)``; (3) enforces ``deny`` spend caps for ``amount`` (admission
        gate); (3b) enforces a ``deny`` ``feature_limit`` for ``feature`` — deny-only
        at admission, exactly like spend caps: the same ledger-derived count as
        :meth:`deduct_with_allowance` (counts prior committed ``usage`` transactions
        tagged ``metadata.feature == feature`` in the window; ``warn``/``notify`` are
        NOT checked here, mirroring how admission only ever enforces ``deny`` spend
        caps); (4) computes ``available = balance − Σ active holds`` and rejects with
        ``error="insufficient_credits"`` if ``available − amount < floor``; (5)
        inserts an ``active`` lease expiring after ``ttl_seconds``. No lease-record
        change is needed for feature limits: since ``create_lease`` never inserts a
        ``usage`` transaction (only :meth:`settle_lease` does), a lease that is later
        released instead of settled was never counted in the first place — nothing
        to undo.

        ``floor`` is the resolved admission floor (``>= 0`` for strict; the negative
        ``overdraft_floor`` for overdraft). ``billing_mode``/``overdraft_floor`` are
        persisted on the lease for settle-time/observability. Business failures are
        returned via ``LeaseResult.error`` (including ``"feature_limit_reached"``);
        the store never raises domain exceptions.

        ``period_start`` (WS9) keys the allowance-headroom lookup for
        ``rolling_30d``/``anniversary`` plans; ``None`` falls back to the current
        UTC calendar month. ``feature``/``feature_limit``/``feature_period_start``
        are resolved by the manager exactly as in :meth:`deduct_with_allowance`;
        ``feature_limit=None`` skips enforcement.
        """
        ...

    @abstractmethod
    def settle_lease(
        self,
        user_id: str,
        lease_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
        skip_allowance: bool = False,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> DeductionResult:
        """Charge the **actual** cost against a lease, then mark it settled (D5).

        De-clamped: charges ``amount`` even if it exceeds the lease hold (overdraft),
        never clamps to the lease amount.
        Pipeline: idempotency replay → allowance consume (skipped when
        ``skip_allowance=True``, Fix 7) → spend-cap (advisory at settle: a breach
        sets ``cap_warning`` but never blocks) → feature-limit (advisory at settle,
        same reasoning: the work already happened, so a breach only sets
        ``feature_limit_warning`` and never blocks) → debit (no floor block; balance
        may go negative in overdraft) → ledger row (tagged ``metadata.feature`` when
        ``feature`` is given — this is what makes the call countable for future
        checks) → mark lease ``settled``. ``amount == 0`` releases the lease without
        charging (and does not tag/count anything).

        ``skip_allowance=True`` prevents the free inference allowance from being
        consumed at settle time — mirrors :meth:`deduct_with_allowance` Fix 7 so
        the lease path and the direct-deduct path behave consistently.

        ``period_start`` (WS9) keys allowance consumption for
        ``rolling_30d``/``anniversary`` plans; ``None`` falls back to the current
        UTC calendar month. ``feature``/``feature_limit``/``feature_period_start``
        mirror :meth:`deduct_with_allowance` — the caller (manager) re-supplies
        ``feature`` at settle time exactly as it already re-supplies ``model`` for
        per-model spend-cap accuracy at settle (no feature name is persisted on the
        lease itself).

        Lease-state failures are returned via ``DeductionResult.error``:
        ``lease_not_found`` (missing / other user / released), ``lease_expired``
        (TTL elapsed — call :meth:`renew_lease` for long jobs). A replayed settle
        (same idempotency key, or a re-settle of an already-settled lease) returns
        the original result with ``idempotent=True``.
        """
        ...

    @abstractmethod
    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        """Release a lease without charging (work failed/aborted).

        Idempotent and safe on missing/already-finalized leases (resolves H1):
        transitions an ``active``/``expired`` lease to ``released`` and reports
        ``released=True``; otherwise reports ``released=False`` with a ``reason``.
        """
        ...

    @abstractmethod
    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        """Extend an active lease's TTL (long batch/agentic jobs, resolves B4).

        Returns ``error="lease_expired"`` if the TTL already elapsed and
        ``error="lease_not_found"`` if missing/other-user/finalized.
        """
        ...

    @abstractmethod
    def get_available(self, user_id: str) -> AvailableResult:
        """Advisory, non-locking read of ``available = balance − Σ active holds``.

        For UI only — never an admission gate (D4/H3); the value may be stale the
        instant it is read.
        """
        ...

    @abstractmethod
    def get_credit_tiers(self, user_id: str) -> TierBalancesResult:
        """Return per-tier balance breakdown for a user, ordered by priority ascending.

        When no tiers are configured, returns a single synthetic ``"default"``
        tier entry so the shape is uniform regardless of whether tiers are
        configured.
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
    def set_user_plan(
        self,
        user_id: str,
        plan_id: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        """Assign a plan to a user.

        ``plan_assigned_at`` anchors the allowance window for
        ``rolling_30d``/``anniversary`` plans. When omitted, the store sets
        the anchor to the current time (backwards-compatible default).
        """
        ...

    @abstractmethod
    def unset_user_plan(self, user_id: str) -> dict:
        """Clear the user's plan (pauses allowance period)."""
        ...

    @abstractmethod
    def check_allowance(self, user_id: str, period_start: date | None = None) -> AllowanceResult:
        """Get remaining free allowance for current billing period.

        ``period_start`` overrides the window key for ``rolling_30d``/``anniversary``
        plans (resolved by the manager via :func:`bursar.allowance.resolve_allowance_window`);
        ``None`` keeps the calendar-month default (WS9).
        """
        ...

    @abstractmethod
    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        """Record allowance consumption for current billing period."""
        ...

    @abstractmethod
    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: date,
        period_end: date,
    ) -> FeatureLimitResult:
        """Advisory, non-locking read of invocation-count usage (UI only).

        Mirrors :meth:`check_spend_cap`/:meth:`check_allowance`: the caller (the
        manager) has already resolved the ``FeatureLimit`` from the user's plan
        and the calendar window via :func:`bursar.allowance.resolve_calendar_window`
        — this method only counts. Counting is ledger-derived (see
        :meth:`deduct_with_allowance`): the number of committed ``usage``
        transactions with ``metadata.feature == feature`` in ``[period_start,
        period_end)``. Never used for admission control.

        Args:
            user_id: The user to check.
            feature: The feature name.
            max_calls: The configured limit (``FeatureLimit.max_calls``).
            period_start: Inclusive window start (resolved by the caller).
            period_end: Exclusive window end (resolved by the caller).

        Returns:
            ``FeatureLimitResult`` with ``limited=True`` and the count/remaining.
        """
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
        user_id: str | None = None,
    ) -> SweepResult:
        """Sweep expired credits from all users' balances, or a single user's.

        Credit expiry is the one time-based mechanism in bursar that isn't
        already lazy-on-read (allowance windows and lease TTLs need no cron).
        ``user_id`` lets a caller (typically the manager layer, before a
        balance-affecting operation) scope the sweep to just that user so it
        can be invoked lazily on the request path instead of requiring an
        operator-maintained cron job. ``user_id=None`` (the default) preserves
        the original global-sweep behavior exactly.

        Args:
            dry_run: If True, report what would be expired without modifying.
            user_id: Optional user to scope the sweep to. When given, only
                that user's expired grants are considered/expired; other
                users' expired grants are left untouched until they are swept
                (globally or individually) themselves.

        Returns:
            ``SweepResult`` with count and amount of expired credits.
        """
        ...

    # ── Usage analytics (optional capability — WS8) ──────────────────────
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

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        """Aggregate statistics across all users in a time window.

        Args:
            start: Start of time window (inclusive).
            end: End of time window (inclusive).

        Returns:
            ``AggregateStatsRow`` with total credits consumed, active users,
            average daily spend, top model, and top user.
        """
        raise CapabilityNotSupportedError("aggregate_stats is not supported by this store")

    # ── Transaction listing (optional capability — WS8) ──────────────────────

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
        raise CapabilityNotSupportedError("list_user_transactions is not supported by this store")

    # ── Team/shared balance pools (optional capability — WS8) ──────────────

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
        raise CapabilityNotSupportedError("create_team is not supported by this store")

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        """Fetch team balance and member count.

        Args:
            team_id: The team's UUID.

        Returns:
            ``TeamBalanceResult`` with balance and member count.
        """
        raise CapabilityNotSupportedError("get_team_balance is not supported by this store")

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
        raise CapabilityNotSupportedError("add_team_member is not supported by this store")

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        """List all members of a team.

        Args:
            team_id: The team's UUID.

        Returns:
            List of ``TeamMember``.
        """
        raise CapabilityNotSupportedError("get_team_members is not supported by this store")

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
        raise CapabilityNotSupportedError("deduct_team is not supported by this store")
