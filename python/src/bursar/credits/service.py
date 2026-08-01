"""High-level credit manager.

Orchestrates the credit lifecycle. The hot "calculate cost then charge now"
path is a single atomic, idempotency-keyed store transaction
(``deduct_with_allowance``) — allowance, entitlement, quota, and debit all
commit (or roll back) together inside the store (contract §2, C1).

Example::

    from bursar import CreditsService, PostgresStore, UsageMetrics

    store = PostgresStore(database_url, tenant_id=tenant_id)
    manager = CreditsService(store=store)

    # One-time setup (creates tables + RPCs)
    # Apply database migrations with the CLI before constructing the service.

    # Load pricing from store (bursar_config table)
    manager.load_pricing_from_store()

    # Deduct credits for a usage event
    result = manager.deduct(
        user_id="user_abc",
        metrics=UsageMetrics(
            operation="completion",
            measures={"input_tokens": 500, "output_tokens": 200},
            dimensions={"model": "claude-opus-4"},
        ),
        idempotency_key="chat_42_turn_7",
    )
    print(f"Deducted {result.amount} credits, balance: {result.balance_after}")
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from bursar.config import ConfigError
from bursar.credits.events import CreditEvent, CreditEventEmitter, CreditEventType
from bursar.credits.service_types import (
    BeginBilledOperationOptions,
    CanAffordOptions,
    CreditsServiceOptions,
    GrantSubscriptionCycleOptions,
    LowBalanceConfig,
    MetricsOrAmount,
    PostDeductionContext,
    PostDeductionSource,
    ReserveOptions,
    RunBilledAsyncOptions,
    RunBilledOptions,
    SettleOptions,
)
from bursar.credits.store import (
    CapReachedError,
    CreateLeaseOptions,
    CreditStore,
    RefundError,
    SettleLeaseOptions,
    StoreError,
)
from bursar.credits.types import (
    AddCreditsResult,
    AggregateStats,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BillingMode,
    BucketBalancesResult,
    BursarConfigResult,
    CanAffordResult,
    CheckFeatureResult,
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
    OperationPolicy,
    PlanMigrationBatchResult,
    PlanMigrationStartResult,
    QuotaEvent,
    QuotaState,
    RefundResult,
    ReleaseResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamDeductionResult,
    TopUserRow,
    UsageAnalyticsStore,
    UsageChargeCursor,
    UsageChargePage,
)
from bursar.engine import PricingEngine
from bursar.errors import (
    ConcurrencyLimitError,
    CreditError,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    OperationNotAllowedError,
    PricingNotLoadedError,
    QuotaExceededError,
)
from bursar.metrics import UsageMetrics
from bursar.retry import BursarRetryOptions, retry_bursar_operation
from bursar.shared.logger import NormalizedLogger, normalize_logger

#: Default lease TTL (seconds) for ``reserve``/``runBilled`` (interface plan §3).
#: Long batch/agentic jobs call :meth:`CreditsService.renew` before this elapses.
DEFAULT_LEASE_TTL_SECONDS = 600

#: Built-in financial-safety presets (interface plan §2). ``strict_prepaid`` keeps
#: the floor ``>= 0`` (structural zero debt); ``overdraft`` permits a negative floor
#: and bills the full actual cost at settle.
POLICY_PRESETS = frozenset({"strict_prepaid", "overdraft"})


class RunBilledResult(BaseModel):
    """Typed result from :meth:`run_billed`."""

    model_config = ConfigDict(extra="forbid")

    result: Any
    deduction: DeductionResult


@dataclass(frozen=True, slots=True)
class BilledOperation:
    """A replay-safe reserve/settle handle for work spanning callbacks."""

    _service: CreditsService
    user_id: str
    lease_id: str
    operation_key: str
    feature: str | None = None
    metadata: CreditMetadata | None = None

    def settle(
        self,
        actual: MetricsOrAmount,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        return self._service.settle(
            self.user_id,
            self.lease_id,
            actual,
            SettleOptions(
                idempotency_key=f"{self.operation_key}:settle",
                feature=self.feature,
                metadata=metadata or self.metadata,
            ),
        )

    def renew(self, ttl: int | None = None) -> None:
        self._service.renew(self.user_id, self.lease_id, ttl)

    def release(self) -> None:
        self._service.release(self.user_id, self.lease_id)


class CreditsService:
    """Orchestrates credit operations: pricing -> atomic deduct.

    Args:
        store: A ``CreditStore`` adapter (e.g. ``PostgresStore``).
        engine: An optional pre-configured ``PricingEngine``. If omitted,
            call ``load_pricing_from_store()`` or ``publish_pricing_from_dict()``
            before ``deduct()``.
        emitter: An optional ``CreditEventEmitter`` for lifecycle events.
        low_balance: An optional :class:`LowBalanceConfig` configuring the
            ``credits.low_balance`` signal (contract §6 / M18 / WS7). When
            ``None`` (the default), no explicit thresholds are configured and
            the threshold defaults to zero.

        pricing_ttl: Milliseconds after which the cached ``PricingEngine`` is
            considered stale and the next call to ``refresh_if_stale()`` will
            reload it from the store. Set to ``0`` to disable auto-reload (the
            consumer must call ``load_pricing_from_store()`` manually).
            Default ``300000`` (5 minutes). Concurrent calls to
            ``refresh_if_stale()`` are safe (the underlying store cache has its
            own stampede protection).
    """

    def __init__(
        self,
        store: CreditStore,
        engine: PricingEngine | None = None,
        emitter: CreditEventEmitter | None = None,
        options: CreditsServiceOptions | None = None,
        *,
        policy: str | None = None,
        overdraft_floor: Decimal | None = None,
        max_concurrent: int | None = None,
        low_balance: LowBalanceConfig | None = None,
        default_ttl_seconds: int | None = None,
        pricing_ttl: int | None = None,
        analytics: UsageAnalyticsStore | None = None,
        lazy_expiry: bool | None = None,
        post_deduction: (Callable[[PostDeductionContext], None | Awaitable[None]] | None) = None,
    ) -> None:
        options = options or CreditsServiceOptions()
        policy = policy if policy is not None else options.policy
        overdraft_floor = overdraft_floor if overdraft_floor is not None else options.overdraft_floor
        max_concurrent = max_concurrent if max_concurrent is not None else options.max_concurrent
        low_balance = low_balance if low_balance is not None else options.low_balance
        default_ttl_seconds = default_ttl_seconds if default_ttl_seconds is not None else options.default_ttl_seconds
        pricing_ttl = pricing_ttl if pricing_ttl is not None else options.pricing_ttl
        analytics = analytics if analytics is not None else options.analytics
        lazy_expiry = lazy_expiry if lazy_expiry is not None else options.lazy_expiry
        post_deduction = post_deduction if post_deduction is not None else options.post_deduction
        if policy not in POLICY_PRESETS:
            raise ValueError(f"unknown policy preset {policy!r}; expected one of {sorted(POLICY_PRESETS)}")
        self._store = store
        self._analytics = analytics or store
        self._engine = engine
        self._emitter = emitter
        self._logger: NormalizedLogger = normalize_logger(options.logger)
        # Financial-safety policy (interface plan §1/§2). ``policy`` is the preset
        # default used for planless users; per-plan / per-call policy layers on top.
        self._policy = policy
        self._overdraft_floor = Decimal(overdraft_floor) if overdraft_floor is not None else None
        self._default_max_concurrent = max_concurrent
        self._default_ttl = default_ttl_seconds
        # Multi-level low_balance thresholds (interface plan §6 / WS7), sorted
        # high→low. None when no LowBalanceConfig (or an empty thresholds list)
        # was supplied -- the threshold is then derived lazily at deduct time.
        self._low_balance_thresholds = (
            sorted((Decimal(t) for t in low_balance.thresholds), reverse=True)
            if low_balance is not None and low_balance.thresholds
            else None
        )
        self._on_low_balance = low_balance.on_trigger if low_balance is not None else None
        # Edge-trigger state: per-user set of thresholds currently breached ("below").
        # A level re-arms only after the balance climbs back above it (a top-up).
        self._lb_below: OrderedDict[str, set[Decimal]] = OrderedDict()
        self._lb_max_tracked_users = low_balance.max_tracked_users if low_balance is not None else 100_000
        self._lb_lock = threading.RLock()
        # Pricing-engine staleness tracking. ``load_pricing_from_store`` /
        # ``publish_pricing`` / ``publish_pricing_from_dict`` bump the timestamp.
        self._pricing_ttl = pricing_ttl / 1_000
        self._lazy_expiry = lazy_expiry
        self._last_loaded: float = 0.0
        # Guards refresh_if_stale() so only one thread calls load_pricing_from_store
        # at a time. Double-checked locking (the time check runs before AND after
        # acquiring the lock) prevents stampede when the TTL expires concurrently.
        self._pricing_refresh_lock = threading.Lock()
        self._version_engines: dict[int, PricingEngine] = {}
        self._post_deduction_hooks: set[Callable[[PostDeductionContext], None | Awaitable[None]]] = set()
        if post_deduction is not None:
            self._post_deduction_hooks.add(post_deduction)

    def add_post_deduction_hook(
        self,
        hook: Callable[[PostDeductionContext], None | Awaitable[None]],
    ) -> Callable[[], None]:
        """Register an awaited, failure-isolated post-commit deduction hook."""
        self._post_deduction_hooks.add(hook)

        def remove() -> None:
            self._post_deduction_hooks.discard(hook)

        return remove

    @staticmethod
    def _wait_for_hook(awaitable: Awaitable[None]) -> None:
        async def wait() -> None:
            await awaitable

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(wait())
            return

        error: list[BaseException] = []

        def runner() -> None:
            try:
                asyncio.run(wait())
            except BaseException as exc:  # pragma: no cover - re-raised below
                error.append(exc)

        worker = threading.Thread(target=runner, daemon=True)
        worker.start()
        worker.join()
        if error:
            raise error[0]

    def _after_deduction(
        self,
        user_id: str,
        source: PostDeductionSource,
        deduction: DeductionResult,
    ) -> None:
        context = PostDeductionContext(
            user_id=user_id,
            source=source,
            deduction=deduction,
        )
        for hook in tuple(self._post_deduction_hooks):
            try:
                result = hook(context)
                if inspect.isawaitable(result):
                    self._wait_for_hook(result)
            except Exception as exc:
                self._logger.warn(
                    "post-deduction hook failed",
                    {
                        "user_id": user_id,
                        "source": source,
                        "error": str(exc),
                    },
                )

    def _engine_for_catalog_version(self, catalog_version: int) -> PricingEngine:
        """Load and cache an immutable historical catalog revision."""
        cached = self._version_engines.get(catalog_version)
        if cached is not None:
            return cached

        cfg = self._store.get_bursar_config(catalog_version)
        if cfg is None or cfg.config is None:
            raise PricingNotLoadedError(f"No pricing config for pinned catalog version {catalog_version}")

        engine = PricingEngine.from_dict(cfg.config if isinstance(cfg.config, dict) else {})
        self._version_engines[catalog_version] = engine
        return engine

    def _engine_for_user(self, user_id: str | None) -> PricingEngine:
        """Return the pricing engine pinned to the user's catalog version."""
        if user_id is None:
            if not self._engine:
                raise PricingNotLoadedError(
                    "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
                )
            return self._engine

        plan = self._store.get_user_plan(user_id)
        catalog_version = plan.catalog_version
        if catalog_version is None:
            self.refresh_if_stale()
            if not self._engine:
                raise PricingNotLoadedError(
                    "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
                )
            return self._engine

        return self._engine_for_catalog_version(catalog_version)

    def _emit(self, type_: CreditEventType, user_id: str, data: dict[str, Any] | None = None) -> None:
        """Emit a credit lifecycle event. No-op if no emitter is configured."""
        if self._emitter:
            self._emitter.emit(
                CreditEvent(
                    type=type_,
                    timestamp=datetime.now(UTC),
                    user_id=user_id,
                    data=data,
                )
            )

    def _emit_quota_events(self, user_id: str, idempotency_key: str) -> None:
        events = self._store.list_quota_events(
            user_id,
            ListQuotaEventsOptions(idempotency_key=idempotency_key, limit=100),
        )
        for event in events:
            data = {
                "quota_key": event.quota_key,
                "operation": event.operation,
                "measure": event.measure,
                "threshold_percent": event.threshold_percent,
                "usage_charge_id": event.usage_charge_id,
                "idempotency_key": event.idempotency_key,
            }
            if event.event_type == "blocked":
                self._emit("credits.quota_blocked", user_id, data)
            elif event.event_type == "threshold":
                self._emit("credits.quota_threshold", user_id, data)

    @staticmethod
    def _to_decimal(value: Decimal | int | float | str) -> Decimal:
        """Safely coerce a value to Decimal, avoiding float precision loss."""
        if isinstance(value, Decimal):
            return value
        if isinstance(value, float):
            return Decimal(str(value))
        return Decimal(value)

    def _resolve_low_balance_threshold(self) -> Decimal:
        """Return the zero default used when no explicit threshold is configured."""
        return Decimal(0)

    # -- Pricing configuration -------------------------------------------

    def publish_pricing_from_dict(self, data: dict[str, Any]) -> None:
        """Load pricing from a raw dict and sync it."""
        from bursar.config import canonical_bursar_config_dict

        canonical = canonical_bursar_config_dict(data)
        engine = PricingEngine.from_dict(canonical)
        self._engine = engine
        self._version_engines.clear()
        self._last_loaded = time.monotonic()
        self._store.set_active_pricing(canonical)

    def load_pricing_from_store(self) -> None:
        """Load the active pricing config from the store."""
        active = self._store.get_active_pricing()
        if active is None:
            raise PricingNotLoadedError(
                "No active pricing config found in the store. "
                "Call publish_pricing_from_dict() or set_active_pricing() first."
            )
        engine_dict = active.config if isinstance(active.config, dict) else {}
        self._engine = PricingEngine.from_dict(engine_dict)
        self._version_engines.clear()
        self._last_loaded = time.monotonic()

    def publish_pricing(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> None:
        """Publish new pricing and update the engine in one call."""
        from bursar.config import canonical_bursar_config_dict

        canonical = canonical_bursar_config_dict(config)
        self._engine = PricingEngine.from_dict(canonical)
        self._version_engines.clear()
        self._last_loaded = time.monotonic()
        self._store.set_active_pricing(canonical, label)

    def publish_pricing_draft(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        """Publish an inactive pricing draft without mutating the live catalog."""
        from bursar.config import canonical_bursar_config_dict

        canonical = canonical_bursar_config_dict(config)
        return self._store.publish_pricing(canonical, label)

    def activate_pricing(self, version: int) -> str:
        """Activate a previously published catalog version and reload it."""
        result = self._store.activate_pricing(version)
        self.load_pricing_from_store()
        return result

    def refresh_if_stale(self) -> None:
        """If the cached ``PricingEngine`` is stale (TTL expired), reload it
        from the store. When ``pricing_ttl`` is ``0`` this is a no-op.

        Double-checked locking prevents stampede when multiple threads observe
        the TTL expiring concurrently — only the first thread past the lock
        calls ``load_pricing_from_store()``; the rest see the updated timestamp
        on re-check and skip.
        """
        if self._pricing_ttl == 0:
            return
        now = time.monotonic()
        if self._last_loaded > 0 and now - self._last_loaded < self._pricing_ttl:
            return
        with self._pricing_refresh_lock:
            now = time.monotonic()
            if self._last_loaded > 0 and now - self._last_loaded < self._pricing_ttl:
                return
            self.load_pricing_from_store()

    def invalidate_pricing(self) -> None:
        """Force the next ``refresh_if_stale()`` call to reload from the store."""
        self._last_loaded = 0.0

    @property
    def pricing_engine(self) -> PricingEngine | None:
        """The current PricingEngine, or None if not loaded."""
        return self._engine

    def get_active_pricing(self) -> BursarConfigResult | None:
        """Fetch the active pricing config directly from the store.

        Unlike load_pricing_from_store (which loads into the engine),
        this returns the raw BursarConfigResult without updating engine state.
        Callers that need the engine should use the engine property or
        load_pricing_from_store.
        """
        return self._store.get_active_pricing()

    # -- Credit operations -----------------------------------------------

    def _maybe_lazy_expire(self, user_id: str) -> None:
        if self._lazy_expiry:
            self._run_sweep(False, user_id)

    def _low_balance_state(self, user_id: str) -> set[Decimal]:
        below = self._lb_below.pop(user_id, set())
        self._lb_below[user_id] = below
        while len(self._lb_below) > self._lb_max_tracked_users:
            self._lb_below.popitem(last=False)
        return below

    def get_balance(self, user_id: str) -> BalanceResult:
        """Get a user's current credit balance."""
        self._maybe_lazy_expire(user_id)
        return self._store.get_balance(user_id)

    def add_credits(
        self,
        user_id: str,
        amount: Decimal | int,
        entry_type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
        bucket: str | None = None,
        idempotency_key: str | None = None,
    ) -> AddCreditsResult:
        """Add credits to a user's account (``amount`` is a ``Decimal``).

        ``bucket`` is an optional bucket key to grant into (see
        :meth:`get_bucket_balances`); omitted resolves to the configured
        ``is_default`` bucket, or ``"default"`` when no buckets are configured.
        """
        result = self._store.add_credits(
            user_id,
            self._to_decimal(amount),
            entry_type,
            metadata,
            expires_at,
            bucket,
            idempotency_key,
        )
        self._emit(
            "credits.added",
            user_id,
            {
                "entry_id": result.entry_id,
                "amount": result.amount,
                "new_balance": result.new_balance,
                "type": entry_type,
            },
        )
        # Re-arm multi-level low_balance: any level the topped-up balance is now back
        # above can fire again on the next descent (interface plan §6).
        if self._low_balance_thresholds:
            with self._lb_lock:
                below = self._low_balance_state(user_id)
                for t in self._low_balance_thresholds:
                    if result.new_balance > t:
                        below.discard(t)
        return result

    def deduct_credits(
        self,
        user_id: str,
        amount: Decimal | int,
        *,
        entry_type: str = "adjustment",
        bucket: str | None = None,
        metadata: CreditMetadata | None = None,
        idempotency_key: str | None = None,
    ) -> AddCreditsResult:
        """Deduct a raw credit amount from a user's account.

        Uses the store's ``add_credits`` with the given ``entry_type`` and a
        negative amount. Use this for refund clawbacks and other administrative
        deductions that bypass the usage-based ``deduct()`` flow.

        Args:
            user_id: The user to deduct from.
            amount: The positive amount to deduct (internally negated).
            entry_type: Semantic label (e.g. ``"adjustment"``, ``"refund"``). Must be
                in the SQL allow-list for negative amounts (currently ``adjustment``, ``refund``).
            bucket: The bucket to deduct from.
            metadata: Extra metadata (Pydantic model, passed through to the store).
        """
        result = self._store.add_credits(
            user_id,
            -self._to_decimal(amount),
            entry_type,
            metadata,
            None,
            bucket,
            idempotency_key,
        )
        self._emit(
            "credits.deducted",
            user_id,
            {
                "entry_id": result.entry_id,
                "amount": result.amount,
                "new_balance": result.new_balance,
                "entry_type": entry_type,
            },
        )
        self._after_deduction(
            user_id,
            "raw",
            DeductionResult(
                entry_id=result.entry_id,
                user_id=user_id,
                amount=abs(result.amount),
                allowance_consumed=Decimal(0),
                balance_after=result.new_balance,
                idempotent=result.idempotent,
            ),
        )
        return result

    def grant_subscription_cycle(
        self,
        user_id: str,
        amount: Decimal | int,
        options: GrantSubscriptionCycleOptions | None = None,
    ) -> AddCreditsResult:
        """Grant a subscription cycle's credits idempotently (safe for webhook redelivery).

        Typical use: a payment-provider webhook (renewal, signup) calls this once
        per cycle. ``idempotency_key`` should be the provider's event id so a
        redelivered webhook is a no-op rather than a double-grant.

        Args:
            user_id: The user whose subscription cycle is renewing.
            amount: The cycle's credit grant (coerced to ``Decimal``).
            options: Typed grant configuration. ``bucket`` is the credit bucket
                to grant into (and, when ``replace_prior`` is enabled,
                to zero out first). Requires a store with that bucket configured
                (see :meth:`get_bucket_balances`) — this is deliberate: buckets are
                what let a subscription grant coexist with, and not clobber,
                credits from other sources (purchases, gifts, ...).
                ``expires_at`` is mutually exclusive
                with ``ttl_days``.
                When ``replace_prior`` is true (the default), any leftover balance in
                ``bucket`` from a prior cycle is expired immediately before the
                new grant lands — a renewal replaces the unused balance rather
                than stacking on top of it.
                When ``plan_key`` is given, the service also calls
                :meth:`set_user_plan`; this
                intentionally re-anchors the allowance window, which is correct
                for a new subscription cycle.
                ``idempotency_key`` should be the provider event id and is passed to the
                store's replay-safe ``add_credits`` so a redelivered webhook
                does not double-grant.

        Returns:
            The ``AddCreditsResult`` for the new cycle's grant.

        Raises:
            ValueError: If both ``expires_at`` and ``ttl_days`` are given.
        """
        options = options or GrantSubscriptionCycleOptions()
        expires_at = options.expires_at
        if expires_at is not None and options.ttl_days is not None:
            raise ValueError("grant_subscription_cycle: expires_at and ttl_days are mutually exclusive")
        if options.ttl_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=options.ttl_days)

        amount_dec = self._to_decimal(amount)

        prior_leftover = Decimal(0)
        if options.replace_prior:
            buckets_before = self.get_bucket_balances(user_id)
            for tb in buckets_before.buckets:
                if tb.bucket_key == options.bucket:
                    prior_leftover = tb.balance
                    break

        result = self._store.add_credits(
            user_id,
            amount_dec,
            type="purchase",
            bucket=options.bucket,
            expires_at=expires_at,
            metadata=options.metadata,
            idempotency_key=options.idempotency_key,
        )

        is_fresh_grant = not result.idempotent
        if options.replace_prior and is_fresh_grant and prior_leftover > 0:
            replace_meta: dict[str, Any] = {"reason": "cycle_replaced"}
            self._store.add_credits(
                user_id,
                -prior_leftover,
                type="adjustment",
                bucket=options.bucket,
                metadata=CreditMetadata(**replace_meta),
            )
            # Reflect the post-replace balance so the returned result is accurate
            # (the grant call above only knows the pre-replace balance).
            result = result.model_copy(update={"new_balance": self.get_balance(user_id).balance})

        if options.plan_key is not None:
            self.set_user_plan(user_id, options.plan_key)

        self._emit(
            "credits.cycle_renewed",
            user_id,
            {
                "entry_id": result.entry_id,
                "amount": amount_dec,
                "new_balance": result.new_balance,
                "bucket": options.bucket,
                "plan_key": options.plan_key,
                "idempotency_key": options.idempotency_key,
            },
        )
        return result

    # -- Plan management ------------------------------------------------

    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        """Assign a plan to a user and emit a ``credits.plan_changed`` event.

        Args:
            user_id: The user to assign the plan to.
            plan_key: The plan key to assign (e.g. ``"pro"``).
            plan_assigned_at: Anchor for plan-assignment policy windows. When
                omitted, the store uses the current time.

        Returns:
            ``SetUserPlanResult`` confirming the assignment.
        """
        result = self._store.set_user_plan(user_id, plan_key, plan_assigned_at=plan_assigned_at)
        self._emit(
            "credits.plan_changed",
            user_id,
            {
                "user_id": user_id,
                "plan_key": plan_key,
                "plan_assigned_at": result.plan_assigned_at,
                "timestamp": datetime.now(UTC),
            },
        )
        return result

    def unset_user_plan(self, user_id: str) -> None:
        """Clear a user's plan (pauses the allowance period).

        Re-assign a plan via :meth:`set_user_plan` to re-anchor the allowance
        window.

        Args:
            user_id: The user whose plan to clear.
        """
        self._store.unset_user_plan(user_id)
        self._emit(
            "credits.plan_changed",
            user_id,
            {
                "user_id": user_id,
                "plan_key": None,
                "timestamp": datetime.now(UTC),
            },
        )

    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> PlanMigrationStartResult:
        """Start a resumable migration between catalog plans."""
        return self._store.start_plan_migration(from_plan_id, to_plan_id)

    def migrate_plan_batch(
        self,
        migration_id: str,
        batch_size: int = 100,
    ) -> PlanMigrationBatchResult:
        """Advance a plan migration by one bounded batch."""
        return self._store.migrate_plan_batch(migration_id, batch_size)

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        """Fetch user's current plan (including feature entitlements)."""
        return self._store.get_user_plan(user_id)

    def get_quota_state(
        self,
        user_id: str,
        quota_key: str | None = None,
    ) -> list[QuotaState]:
        """Return current quota windows for a user."""
        return self._store.get_quota_state(user_id, quota_key)

    def list_quota_events(
        self,
        user_id: str,
        options: ListQuotaEventsOptions | None = None,
    ) -> list[QuotaEvent]:
        """List persisted quota threshold and blocking events."""
        return self._store.list_quota_events(user_id, options)

    def check_feature(self, user_id: str, feature: str) -> CheckFeatureResult:
        """Check whether a user's plan has a specific feature entitlement.

        Convenience wrapper around the store's ``check_feature()`` — inspect the
        features dict on a user's plan to gate functionality.

        Presence is distinguished from truthiness (contract §5, M6): a feature is
        present when its key exists and the value is not ``None``/``False``.
        Numeric ``0`` and empty string ``""`` are therefore *present*.
        - absent / ``None`` / ``False`` => ``has_feature=False``
        - ``True`` / numeric (incl. ``0``) / string (incl. ``""``) => ``has_feature=True``
        """
        return self._store.check_feature(user_id, feature)

    def revoke_credits_by_entry_type(self, user_id: str, entry_type: str) -> dict[str, Any]:
        """Revoke all credits of a given transaction type for a user (LIFO across tiers).

        Used by the subscription lifecycle to replace cycle-grant credits on renewal.
        Returns ``{"user_id": ..., "amount": ..., "new_balance": ..., "bucket": ...}``.

        Note: the JS equivalent does **not** emit a ``credits.revoked`` event —
        it delegates directly to the store with no event emission. This divergence
        is intentional (Python adds observability).
        """
        result = self._store.revoke_credits_by_entry_type(user_id, entry_type)
        amount = abs(Decimal(str(result.get("amount", 0))))
        if amount > 0:
            self._emit(
                "credits.revoked",
                user_id,
                {
                    "user_id": user_id,
                    "amount": str(amount),
                    "entry_type": entry_type,
                },
            )
        return result

    def execute_grant_program(
        self,
        request: ExecuteGrantProgramRequest,
    ) -> list[GrantProgramAwardResult]:
        """Execute an application-driven catalog grant program."""
        return self._store.execute_grant_program(request)

    # ── Lease lifecycle: atomic admission (interface plan §3/§4) ────────

    def _preset_policy(self) -> OperationPolicy:
        """The default :class:`OperationPolicy` from the constructor preset (§2)."""
        if self._policy == "overdraft":
            return OperationPolicy(
                billing_mode="overdraft",
                max_concurrent=self._default_max_concurrent,
                overdraft_floor=self._overdraft_floor if self._overdraft_floor is not None else Decimal(0),
            )
        return OperationPolicy(
            billing_mode="strict",
            max_concurrent=self._default_max_concurrent,
            overdraft_floor=None,
        )

    def _resolve_policy(
        self,
        user_id: str,
        operation_type: str,
        billing_mode_override: BillingMode | None = None,
    ) -> OperationPolicy:
        """Resolve the effective policy: explicit arg → catalog plan → preset (§1).

        A **planless** user (``plan_id`` is ``None``) always gets the constructor
        preset, never silently unlimited (resolves M1). A user *with* a plan gets
        its canonical credit and admission policies, then the explicit per-call
        ``billing_mode``.
        """
        policy = self._preset_policy()

        # Intentionally not catching exceptions: a store outage at plan-fetch time
        # must surface to the caller rather than silently demoting the user to the
        # constructor preset (which can flip a paid/overdraft user to strict_prepaid
        # and block legitimate requests without any signal — Fix 4).
        plan = self._store.get_user_plan(user_id)

        if plan is not None and plan.plan_id:
            credit_policy = plan.credit_policy
            billing_mode: BillingMode = (
                "overdraft" if credit_policy is not None and credit_policy.type == "credit_line" else "strict"
            )
            overdraft_floor = (
                -credit_policy.credit_limit
                if credit_policy is not None
                and credit_policy.type == "credit_line"
                and credit_policy.credit_limit is not None
                else None
            )
            admission = plan.admission
            max_concurrent = (
                admission.max_in_flight
                if admission is not None and admission.max_in_flight is not None
                else policy.max_concurrent
            )
            operation_admission = admission.operations.get(operation_type) if admission is not None else None
            if operation_admission is not None and operation_admission.max_in_flight is not None:
                max_concurrent = operation_admission.max_in_flight
            policy = OperationPolicy(
                billing_mode=billing_mode,
                max_concurrent=max_concurrent,
                overdraft_floor=overdraft_floor,
            )

        if billing_mode_override is not None:
            policy = policy.model_copy(update={"billing_mode": billing_mode_override})
        return policy

    def _resolve_floor(self, policy: OperationPolicy) -> Decimal:
        """Admission floor for a policy: its credit-line floor, otherwise zero."""
        if policy.billing_mode == "overdraft":
            return policy.overdraft_floor if policy.overdraft_floor is not None else Decimal(0)
        return Decimal(0)

    def _cost_of(
        self,
        metrics_or_amount: UsageMetrics | Decimal | int,
        user_id: str | None = None,
        *,
        lease_id: str | None = None,
    ) -> tuple[Decimal, str | None]:
        """Compute a credit cost, pinning leased work to its admission catalog."""
        if not isinstance(metrics_or_amount, UsageMetrics):
            return Decimal(metrics_or_amount), None

        pricing_context: LeasePricingContext | None = None
        if lease_id is not None:
            if user_id is None:
                raise ValueError("user_id is required when pricing a lease")
            pricing_context = self._store.get_lease_pricing_context(user_id, lease_id)
            if pricing_context is None:
                raise LeaseNotFoundError(f"Lease not found. User={user_id}")
            engine = self._engine_for_catalog_version(pricing_context.catalog_version)
            rate_card = pricing_context.rate_card or engine.get_rate_card_for_plan(pricing_context.plan_key)
        else:
            engine = self._engine_for_user(user_id)
            rate_card = None
        if user_id is not None and pricing_context is None:
            plan = self._store.get_user_plan(user_id)
            rate_card = engine.get_rate_card_for_plan(plan.plan_key)
        breakdown = engine.calculate(metrics_or_amount, rate_card=rate_card)
        model = metrics_or_amount.dimensions.get("model")
        return breakdown.total, str(model) if model is not None else None

    def _raise_lease_error(self, error: str, user_id: str, amount: Decimal) -> None:
        """Map a store business code to the coherent typed exception (M2)."""
        if error in ("concurrency_limit", "max_concurrent_reached"):
            raise ConcurrencyLimitError(f"Concurrency limit reached. User={user_id}")
        if error == "quota_exceeded":
            raise QuotaExceededError(f"Usage quota exceeded. User={user_id}")
        if error == "feature_not_entitled":
            raise FeatureNotEntitledError(f"Feature not entitled. User={user_id}")
        if error == "operation_not_allowed":
            raise OperationNotAllowedError(f"Operation is not allowed. User={user_id}")
        if error in ("insufficient_credits", "insufficient_headroom"):
            raise InsufficientCreditsError(f"Insufficient credits. User={user_id}, requested={amount}")
        if error in ("lease_expired", "expired_lease"):
            raise LeaseExpiredError(f"Lease expired. User={user_id}")
        if error in ("lease_not_found", "not_found", "missing_lease"):
            raise LeaseNotFoundError(f"Lease not found. User={user_id}")
        if error in ("released_lease", "settled_lease"):
            raise LeaseNotFoundError(f"Lease is already finalized. User={user_id}")
        if error in ("missing_quota_measure", "invalid_measure", "policy_mismatch"):
            raise ConfigError(f"Invalid operation policy for user {user_id}: {error}")
        if error in ("invalid_amount", "invalid_request"):
            raise ValueError(f"Invalid amount: {amount}")
        raise CreditError(f"Operation failed: {error}. User={user_id}")

    def reserve(
        self,
        user_id: str,
        metrics_or_amount: MetricsOrAmount,
        options: ReserveOptions | None = None,
    ) -> LeaseResult:
        """Atomically acquire a lease — the only admission control (D4).

        Resolves the effective policy, enforces ``feature``, sizes the hold
        from ``metrics_or_amount`` (worst-case in strict, estimate in overdraft — the
        caller chooses what to pass), and calls the store's atomic ``create_lease``.

        The store's ``create_lease`` is allowance-aware: remaining free allowance is
        added to the effective headroom so free-tier users are not falsely rejected
        for worst-case holds they can cover with allowance (Fix 1 / D4).

        ``model`` is inferred from ``UsageMetrics`` when passed; for raw
        ``Decimal``/``int`` amounts use ``options.model`` so quota checks and
        analytics remain accurate (Fix 5).

        ``feature`` is the canonical entitlement key checked at admission.

        On any business failure raises the coherent typed exception; on success emits
        ``credits.reserved`` and returns the :class:`LeaseResult`.
        """
        options = options or ReserveOptions()
        effective_operation = (
            options.operation_type
            if options.operation_type is not None
            else metrics_or_amount.operation
            if isinstance(metrics_or_amount, UsageMetrics)
            else "usage"
        )
        policy = self._resolve_policy(
            user_id,
            effective_operation,
            options.billing_mode,
        )
        floor = self._resolve_floor(policy)
        amount, derived_model = self._cost_of(metrics_or_amount, user_id)
        # When caller passes a raw Decimal/int (no model in metrics), fall back to
        # the explicit ``model`` kwarg so cap checks and analytics are not blind.
        effective_model = derived_model if derived_model is not None else options.model
        ttl_seconds = options.ttl if options.ttl is not None else self._default_ttl
        measures = dict(metrics_or_amount.measures) if isinstance(metrics_or_amount, UsageMetrics) else {}
        dimensions = dict(metrics_or_amount.dimensions) if isinstance(metrics_or_amount, UsageMetrics) else {}
        lease_idempotency_key = options.idempotency_key or f"lease:{uuid4()}"

        result = self._store.create_lease(
            user_id,
            amount,
            effective_operation,
            CreateLeaseOptions(
                billing_mode=policy.billing_mode,
                floor=floor,
                max_concurrent=policy.max_concurrent,
                ttl_seconds=ttl_seconds,
                model=effective_model,
                overdraft_floor=policy.overdraft_floor,
                metadata=options.metadata,
                feature=options.feature,
                idempotency_key=lease_idempotency_key,
                measures=measures,
                dimensions=dimensions,
            ),
        )

        if result.error:
            self._emit(
                "credits.deduct_failed",
                user_id,
                {
                    "error": result.error,
                    "amount": amount,
                    "stage": "reserve",
                    "operation_type": effective_operation,
                },
            )
            if result.error == "quota_exceeded":
                self._emit_quota_events(user_id, lease_idempotency_key)
            self._raise_lease_error(result.error, user_id, amount)

        self._emit(
            "credits.reserved",
            user_id,
            {
                "lease_id": result.lease_id,
                "amount": result.amount,
                "available": result.available,
                "billing_mode": result.billing_mode,
                "operation_type": effective_operation,
                "expires_at": result.expires_at,
            },
        )
        self._emit_quota_events(user_id, lease_idempotency_key)
        return result

    def settle(
        self,
        user_id: str,
        lease_id: str,
        metrics_or_amount: MetricsOrAmount,
        options: SettleOptions | None = None,
    ) -> DeductionResult:
        """Charge the ACTUAL cost against a lease and finalize it (D5).

        De-clamped: bills the full actual cost even if it exceeds the lease hold
        (overdraft). Emits ``credits.deducted``, then low-balance and overdraft
        signals as applicable. ``feature`` re-supplies the entitlement key passed
        to :meth:`reserve`.
        """
        options = options or SettleOptions()
        amount, model = self._cost_of(
            metrics_or_amount,
            user_id,
            lease_id=lease_id,
        )
        effective_idempotency_key = options.idempotency_key or f"lease:{lease_id}:settle"

        if isinstance(metrics_or_amount, UsageMetrics):
            tx_meta = self._build_tx_metadata(
                metrics_or_amount,
                amount,
                effective_idempotency_key,
                options.metadata,
            )
            measures = dict(metrics_or_amount.measures)
            dimensions = dict(metrics_or_amount.dimensions)
        else:
            base: dict[str, Any] = options.metadata.model_dump(exclude_none=True) if options.metadata else {}
            base["idempotency_key"] = effective_idempotency_key
            tx_meta = CreditMetadata(**base)
            measures = {}
            dimensions = {}

        result = self._store.settle_lease(
            user_id,
            lease_id,
            amount,
            SettleLeaseOptions(
                idempotency_key=effective_idempotency_key,
                model=model,
                metadata=tx_meta,
                feature=options.feature,
                measures=measures,
                dimensions=dimensions,
            ),
        )

        if result.error:
            self._emit(
                "credits.deduct_failed",
                user_id,
                {"error": result.error, "amount": amount, "stage": "settle", "lease_id": lease_id},
            )
            if result.error == "quota_exceeded":
                self._emit_quota_events(user_id, effective_idempotency_key)
            if result.error in ("lease_expired", "expired_lease"):
                self._emit("credits.lease_expired", user_id, {"lease_id": lease_id})
            self._raise_lease_error(result.error, user_id, amount)

        self._emit(
            "credits.deducted",
            user_id,
            {
                "entry_id": result.entry_id,
                "amount": result.amount,
                "allowance_consumed": result.allowance_consumed,
                "balance_after": result.balance_after,
                "model": model,
                "lease_id": lease_id,
                "idempotent": result.idempotent,
            },
        )

        self._post_charge_signals(user_id, result)
        if not result.idempotent:
            self._emit_quota_events(user_id, effective_idempotency_key)
            self._after_deduction(user_id, "settle", result)
        return result

    def release(self, user_id: str, lease_id: str) -> ReleaseResult:
        """Release a lease without charging (work failed/aborted) — idempotent (H1)."""
        result = self._store.release_lease(user_id, lease_id)
        if result.released:
            self._emit(
                "credits.reservation_released",
                user_id,
                {"lease_id": lease_id, "reason": result.reason},
            )
        return result

    def renew(self, user_id: str, lease_id: str, ttl: int | None = None) -> LeaseResult:
        """Extend an active lease without changing its captured policy snapshot."""
        ttl_seconds = ttl if ttl is not None else self._default_ttl
        if ttl_seconds < 1:
            raise ValueError("ttl must be a positive integer")
        result = self._store.renew_lease(user_id, lease_id, ttl_seconds)
        if result.error:
            if result.error in ("lease_expired", "expired_lease"):
                self._emit("credits.lease_expired", user_id, {"lease_id": lease_id})
            self._raise_lease_error(result.error, user_id, Decimal(0))
        return result

    def can_afford(
        self,
        user_id: str,
        metrics_or_amount: MetricsOrAmount,
        options: CanAffordOptions | None = None,
    ) -> CanAffordResult:
        """Advisory affordability check — UI only, non-locking, may be stale (D4/H3).

        ``spendable`` in the result reflects the user's effective spending power:
        ``balance − active holds + allowance_remaining − protected_floor``.
        This matches the headroom
        ``reserve`` uses so the Send-button check agrees with the admission gate
        (Fix 1). Never use this as an admission gate; only ``reserve`` is authoritative.
        """
        options = options or CanAffordOptions()
        feature = options.feature
        worst_case, _ = self._cost_of(metrics_or_amount, user_id)
        avail = self._store.get_available(user_id)

        # can_afford() is an advisory / UI method — it must never raise (#7).
        # _resolve_policy may call store.get_user_plan; wrap it so a transient
        # store outage returns a cautious affordable=False rather than an exception.
        try:
            policy = self._resolve_policy(
                user_id,
                options.operation_type,
                options.billing_mode,
            )
        except Exception:
            return CanAffordResult(
                affordable=False,
                spendable=avail.available,
                worst_case=worst_case,
                reason="policy_unavailable",
            )
        floor = self._resolve_floor(policy)

        # Include remaining free allowance in the effective spendable amount so the
        # advisory check agrees with what create_lease will actually admit (Fix 1).
        allowance_credit = Decimal(0)
        try:
            ar = self._store.check_allowance(user_id)
            allowance_credit = ar.allowance_remaining
        except Exception as exc:
            self._logger.debug(
                "allowance fetch failed in can_afford",
                {"error": str(exc)},
            )  # advisory: fail open

        spendable = avail.available + allowance_credit - floor

        affordable = True
        reason: str | None = None
        if feature is not None:
            check = self._store.check_feature(user_id, feature)
            if not check.has_feature:
                affordable = False
                reason = "feature_not_entitled"
        if affordable and spendable < worst_case:
            affordable = False
            reason = "insufficient_credits"

        return CanAffordResult(
            affordable=affordable,
            spendable=spendable,
            worst_case=worst_case,
            reason=reason,
        )

    def get_available(self, user_id: str) -> AvailableResult:
        """Advisory ``available = balance − Σ active holds`` read (UI only, D4/H3)."""
        return self._store.get_available(user_id)

    def get_bucket_balances(self, user_id: str) -> BucketBalancesResult:
        """Per-bucket balance breakdown for a user (pure read, no event — matches
        :meth:`get_balance`/:meth:`get_available`)."""
        return self._store.get_bucket_balances(user_id)

    def check_allowance(self, user_id: str) -> AllowanceResult:
        """Get the database-owned current allowance window."""
        return self._store.check_allowance(user_id)

    def run_billed(
        self,
        user_id: str,
        options: RunBilledOptions,
    ) -> RunBilledResult:
        """One-call shortcut wiring reserve → do_work → settle (interface plan §4).

        ``options.do_work`` runs the operation and returns ``(result, actual)`` where
        ``actual`` is the real usage metrics (or amount) to settle. On any exception
        from ``do_work`` the lease is released and the error re-raised. For long jobs
        ``do_work`` may call :meth:`renew`. A crash between reserve and settle is
        covered by the lease TTL (and the store's reaper).

        ``feature`` names an entitlement required by both admission and
        settlement. The database remains the authoritative policy gate.
        """
        operation_key = options.operation_key or f"billed:{uuid4()}"
        operation = self.begin_billed_operation(
            user_id,
            BeginBilledOperationOptions(
                estimate=options.estimate,
                operation_key=operation_key,
                operation_type=options.operation_type,
                billing_mode=options.billing_mode,
                ttl=options.ttl,
                feature=options.feature,
                metadata=options.metadata,
            ),
        )
        try:
            work_result, actual = options.do_work()
        except Exception:
            operation.release()
            raise

        # Never release after successful work: a failed settle may have an
        # unknown commit outcome and is replay-safe through operation_key.
        deduction = retry_bursar_operation(
            operation.settle,
            actual,
            retry_options=BursarRetryOptions(max_attempts=options.settlement_attempts),
        )
        return RunBilledResult(result=work_result, deduction=deduction)

    async def run_billed_async(
        self,
        user_id: str,
        options: RunBilledAsyncOptions,
    ) -> RunBilledResult:
        """Async counterpart to :meth:`run_billed`."""

        operation_key = options.operation_key or f"billed:{uuid4()}"
        operation = self.begin_billed_operation(
            user_id,
            BeginBilledOperationOptions(
                estimate=options.estimate,
                operation_key=operation_key,
                operation_type=options.operation_type,
                billing_mode=options.billing_mode,
                ttl=options.ttl,
                feature=options.feature,
                metadata=options.metadata,
            ),
        )
        try:
            work_result, actual = await options.do_work()
        except Exception:
            operation.release()
            raise
        deduction = retry_bursar_operation(
            operation.settle,
            actual,
            retry_options=BursarRetryOptions(max_attempts=options.settlement_attempts),
        )
        return RunBilledResult(result=work_result, deduction=deduction)

    def begin_billed_operation(
        self,
        user_id: str,
        options: BeginBilledOperationOptions,
    ) -> BilledOperation:
        """Acquire a replay-safe lease for a complete billable operation."""

        lease = self.reserve(
            user_id,
            options.estimate,
            ReserveOptions(
                operation_type=options.operation_type,
                billing_mode=options.billing_mode,
                ttl=options.ttl,
                feature=options.feature,
                metadata=options.metadata,
                idempotency_key=f"{options.operation_key}:reserve",
            ),
        )
        return BilledOperation(
            _service=self,
            user_id=user_id,
            lease_id=lease.lease_id,
            operation_key=options.operation_key,
            feature=options.feature,
            metadata=options.metadata,
        )

    def resume_billed_operation(
        self,
        user_id: str,
        lease_id: str,
        operation_key: str,
        *,
        feature: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> BilledOperation:
        """Recreate a handle from durable callback/job state."""

        if not operation_key:
            raise ValueError("operation_key must not be empty")
        return BilledOperation(
            _service=self,
            user_id=user_id,
            lease_id=lease_id,
            operation_key=operation_key,
            feature=feature,
            metadata=metadata,
        )

    # ── Low-balance / overdraft signals (interface plan §6) ─────────────

    def _post_charge_signals(self, user_id: str, result: DeductionResult) -> None:
        """Emit overdraft, floor-breach, and multi-level low_balance after a charge.

        Overdraft (balance < 0) is always signalled. Explicit low-balance
        thresholds are non-blocking operational signals.

        Idempotent replays are skipped entirely at the top: re-emitting overdraft
        or floor_breach with the *original* balance figures against the *current*
        live balance would produce spurious duplicate events (Fix 2/#2).
        """
        if result.idempotent:
            return

        if result.balance_after < 0:
            self._emit("credits.overdraft", user_id, {"balance": result.balance_after, "amount": result.amount})

        # balance_before must account for BOTH the net charge (result.amount) AND any
        # free-allowance consumption (result.allowance_consumed).  result.amount is the
        # net debit to the balance; allowance does not touch the balance, so:
        #   balance_before = balance_after + net  (always correct, unchanged)
        # This comment exists to document that allowance_consumed is intentionally
        # excluded: balance only moves by net (Fix #3).
        balance_after = result.balance_after
        balance_before = balance_after + result.amount
        self._emit_low_balance(user_id, balance_before, balance_after)

    def _emit_low_balance(self, user_id: str, balance_before: Decimal, balance_after: Decimal) -> None:
        """Edge-triggered low_balance: multi-level if configured, else single (§6)."""
        if self._low_balance_thresholds:
            with self._lb_lock:
                below = self._low_balance_state(user_id)
                newly_crossed: list[Decimal] = []
                for t in self._low_balance_thresholds:  # high → low
                    if balance_after <= t:
                        if t not in below:
                            below.add(t)
                            newly_crossed.append(t)
                    else:
                        below.discard(t)
                fire_level = min(newly_crossed) if newly_crossed else None
            if fire_level is not None:
                self._fire_low_balance(user_id, balance_after, fire_level)
            return

        threshold = self._resolve_low_balance_threshold()
        if balance_before > threshold >= balance_after:
            self._fire_low_balance(user_id, balance_after, threshold)

    def _fire_low_balance(self, user_id: str, balance: Decimal, threshold: Decimal) -> None:
        """Emit ``credits.low_balance`` and invoke the non-blocking ``on_low_balance``."""
        data = {"balance": balance, "threshold": threshold}
        self._emit("credits.low_balance", user_id, data)
        if self._on_low_balance is not None:
            event = CreditEvent(type="credits.low_balance", timestamp=datetime.now(UTC), user_id=user_id, data=data)
            try:
                result = self._on_low_balance(event)
                if inspect.isawaitable(result):
                    self._wait_for_hook(result)
            except Exception as exc:  # never block/break the op on a handler failure (§6/H4)
                self._logger.error(
                    "on_low_balance handler failed",
                    {"user_id": user_id, "error": str(exc)},
                )

    def _build_tx_metadata(
        self,
        metrics: UsageMetrics,
        breakdown_total: Decimal,
        idempotency_key: str | None,
        metadata: CreditMetadata | None,
    ) -> CreditMetadata:
        """Build ledger metadata: caller fields first, system fields last.

        System-owned keys (``idempotency_key``, ``model``, ``breakdown_total``)
        are applied after caller metadata so they always win (contract §5, M7).
        """
        base: dict[str, Any] = {}
        # Caller metadata first — system fields below overwrite any collisions.
        if metadata:
            base.update(metadata.model_dump(exclude_none=True))
        # System fields last (M7): these must not be overwritten by the caller.
        base["operation"] = metrics.operation
        base["measures"] = {key: str(value) for key, value in metrics.measures.items()}
        base["dimensions"] = dict(metrics.dimensions)
        base["breakdown_total"] = str(breakdown_total)
        if idempotency_key:
            base["idempotency_key"] = idempotency_key
        return CreditMetadata(**base)

    def _raise_deduct_error(
        self,
        error: str,
        user_id: str,
        cost: Decimal,
        metrics: UsageMetrics,
        feature: str | None = None,
    ) -> None:
        self._emit(
            "credits.deduct_failed",
            user_id,
            {
                "amount": cost,
                "model": metrics.dimensions.get("model"),
                "error": error,
                "feature": feature,
            },
        )
        if error == "quota_exceeded":
            raise QuotaExceededError(f"Usage quota exceeded. User={user_id}")
        if error == "feature_not_entitled":
            raise FeatureNotEntitledError(f"Feature not entitled. User={user_id}")
        if error == "operation_not_allowed":
            raise OperationNotAllowedError(f"Operation is not allowed. User={user_id}")
        if error in {"missing_quota_measure", "invalid_measure", "policy_mismatch"}:
            raise ConfigError(f"Deduction configuration is invalid: {error}. User={user_id}")
        if error in {"insufficient_credits", "insufficient_headroom"}:
            raise InsufficientCreditsError(f"Insufficient credits. User={user_id}, requested={cost}")
        if error in {"invalid_amount", "invalid_request"}:
            raise ValueError(f"Invalid deduction amount: {cost}")
        raise StoreError(f"Credit deduction failed: {error}. User={user_id}, requested={cost}")

    def deduct(
        self,
        user_id: str,
        metrics: UsageMetrics,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
        *,
        feature: str | None = None,
    ) -> DeductionResult:
        """Calculate the cost and charge it in one atomic store transaction.

        The flow is thin: ``breakdown = engine.calculate(metrics)`` →
        ``cost = breakdown.total`` (a ``Decimal``, charged exactly with **no**
        truncation) → ``store.deduct_with_allowance(...)``. Usage recording,
        allowance consumption,
        entitlement, quota enforcement, and the debit all commit (or
        roll back) together inside the store (contract §2, C1). The manager only
        maps the returned ``error`` code to a typed exception and emits events.

        Args:
            user_id: The user to charge.
            metrics: Usage metrics (model, tokens, tool calls, etc.).
            idempotency_key: Optional user-scoped key for idempotent replay.
            metadata: Extra metadata to attach to the transaction.
            feature: Optional entitlement key checked by the store.

        Returns:
            ``DeductionResult`` whose ``amount`` is the net (positive) charge to
            the balance after free allowance.

        Raises:
            PricingNotLoadedError: If pricing hasn't been loaded.
            InsufficientCreditsError: If the balance floor would be breached.
        """
        self._maybe_lazy_expire(user_id)
        # 1) Calculate cost — exact Decimal, NO truncation (H1).
        engine = self._engine_for_user(user_id)
        plan = self._store.get_user_plan(user_id)
        breakdown = engine.calculate(metrics, rate_card=plan.rate_card)
        cost = breakdown.total

        # 2) One atomic transaction records zero-cost usage too, so
        # authorization, quotas, and usage history cannot be bypassed by a free
        # rate.
        effective_idempotency_key = idempotency_key or f"usage:{uuid4()}"
        tx_meta = self._build_tx_metadata(
            metrics,
            breakdown.total,
            effective_idempotency_key,
            metadata,
        )
        model_dimension = metrics.dimensions.get("model")
        region_dimension = metrics.dimensions.get("region")
        result = self._store.deduct_with_allowance(
            user_id,
            cost,
            idempotency_key=effective_idempotency_key,
            operation=metrics.operation,
            feature=feature,
            model=str(model_dimension) if model_dimension is not None else None,
            region=str(region_dimension) if region_dimension is not None else None,
            measures=dict(metrics.measures),
            dimensions=dict(metrics.dimensions),
            metadata=tx_meta,
        )

        # 4) Error path: emit a failure event and raise the typed exception.
        #    Never emit a success event here.
        if result.error:
            if result.error == "quota_exceeded":
                self._emit_quota_events(user_id, effective_idempotency_key)
            self._raise_deduct_error(result.error, user_id, cost, metrics, feature)

        # 5) Success path.
        self._emit(
            "credits.deducted",
            user_id,
            {
                "entry_id": result.entry_id,
                "amount": result.amount,
                "allowance_consumed": result.allowance_consumed,
                "balance_after": result.balance_after,
                "model": metrics.dimensions.get("model"),
                "idempotent": result.idempotent,
            },
        )

        # Edge-triggered low_balance (M18): multi-level if configured (WS7), else
        # single-threshold — see _emit_low_balance for the shared logic used by
        # both the direct-deduct path and the lease/settle path.
        if not result.idempotent:
            balance_before = result.balance_after + result.amount
            self._emit_low_balance(user_id, balance_before, result.balance_after)
            self._emit_quota_events(user_id, effective_idempotency_key)
            self._after_deduction(user_id, "deduct", result)

        return result

    def deduct_flat_job(
        self,
        user_id: str,
        job_name: str,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
        feature: str | None = None,
    ) -> DeductionResult:
        """Deduct the configured fixed cost for one named job."""
        return self.deduct(
            user_id,
            UsageMetrics(operation=job_name, measures={"jobs": Decimal(1)}),
            idempotency_key,
            metadata,
            feature=feature,
        )

    def refund_credits(
        self,
        entry_id: str,
        amount: Decimal | int | None = None,
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
            idempotency_key: Stable replay key. Omitted full refunds use a
                deterministic key derived from the original entry.

        Returns:
            ``RefundResult`` with the refund ledger entry details.

        Raises:
            RefundError: On over-refund, duplicate, wrong-type, or missing-entry
                failures. ``credits.refund_failed`` is emitted and no success
                event fires.
        """
        refund_amount = self._to_decimal(amount) if amount is not None else None
        result = self._store.refund_credits(
            entry_id,
            refund_amount,
            reason,
            metadata,
            idempotency_key,
        )

        # Check the error BEFORE emitting (H3): a failed/duplicate/over-refund
        # must never fire a success event.
        if result.error:
            self._emit(
                "credits.refund_failed",
                result.user_id,
                {
                    "entry_id": entry_id,
                    "error": result.error,
                    "reason": reason,
                },
            )
            raise RefundError(f"Refund rejected: {result.error}")

        self._emit(
            "credits.refunded",
            result.user_id,
            {
                "entry_id": entry_id,
                "refund_entry_id": result.refund_entry_id,
                "amount": result.amount,
                "new_balance": result.new_balance,
                "reason": reason,
            },
        )
        return result

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        metrics: UsageMetrics,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> TeamDeductionResult:
        """Deduct from a team's shared balance pool.

        Calculates cost via the pricing engine, then debits the team pool.

        Args:
            team_id: The team's UUID.
            user_id: The user to attribute the deduction to.
            metrics: Usage metrics (model, tokens, etc.).
            idempotency_key: Optional idempotency key.
            metadata: Extra metadata.

        Returns:
            ``TeamDeductionResult`` with ledger entry details.
        """
        self._maybe_lazy_expire(user_id)
        engine = self._engine_for_user(user_id)
        plan = self._store.get_user_plan(user_id)
        breakdown = engine.calculate(metrics, rate_card=plan.rate_card)
        cost = breakdown.total  # exact Decimal, no truncation (H1)

        if cost <= 0:
            team_bal = self._store.get_team_balance(team_id)
            return TeamDeductionResult(
                entry_id="",
                team_id=team_id,
                user_id=user_id,
                amount=Decimal(0),
                team_balance_after=team_bal.balance,
            )

        team_metadata_data = metadata.model_dump(exclude_none=True) if metadata else {}
        team_metadata_data.update(
            {
                "operation": metrics.operation,
                "measures": dict(metrics.measures),
                "dimensions": {key: str(value) for key, value in metrics.dimensions.items()},
                "breakdown_total": str(breakdown.total),
            }
        )
        team_metadata = CreditMetadata(**team_metadata_data)
        result = self._store.deduct_team(
            team_id,
            user_id,
            cost,
            team_metadata,
            idempotency_key=idempotency_key,
        )

        # Consistent with deduct() (H3): on error emit a failure event and raise
        # rather than returning a silent error result.
        if result.error:
            self._emit(
                "credits.deduct_failed",
                user_id,
                {
                    "error": result.error,
                    "amount": cost,
                    "team_id": team_id,
                    "deduct_type": "team",
                },
            )
            if result.error == "member_spend_cap_exceeded":
                raise CapReachedError(
                    f"Team member spend cap exceeded. Team={team_id}, user={user_id}, requested={cost}"
                )
            raise InsufficientCreditsError(
                f"Team deduction failed: {result.error}. Team={team_id}, user={user_id}, requested={cost}"
            )

        self._emit(
            "credits.deducted",
            user_id,
            {
                "entry_id": result.entry_id,
                "amount": result.amount,
                "team_balance_after": result.team_balance_after,
                "team_id": team_id,
                "deduct_type": "team",
            },
        )
        return result

    def _run_sweep(self, dry_run: bool, user_id: str | None = None) -> SweepResult:
        """Run one bounded expiry-worker batch."""
        result = self._store.sweep_expired_credits(dry_run=dry_run, user_id=user_id)
        if not dry_run and result.expired_count > 0:
            self._emit(
                "credits.expired",
                user_id or "system",
                {
                    "expired_count": result.expired_count,
                    "expired_amount": result.expired_amount,
                    "expired_by_bucket": result.expired_by_bucket,
                },
            )
        return result

    def sweep_expired_credits(
        self,
        dry_run: bool = False,
    ) -> SweepResult:
        """Inspect or expire eligible credit lots."""
        return self._run_sweep(dry_run)

    # ── Usage analytics ─────────────────────────────────────────────────

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Aggregate spend by user in a time window."""
        return self._analytics.spend_by_user(start, end)

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Aggregate spend by model in a time window."""
        return self._analytics.spend_by_model(start, end)

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        """Top users by spend in a time window."""
        return self._analytics.top_users(limit, start, end)

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        """Daily spend aggregation in a time window."""
        return self._analytics.daily_spend(start, end)

    def list_ledger_entries(
        self,
        user_id: str,
        entry_types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage:
        """List account ledger history with a stable cursor."""
        return self._store.list_ledger_entries(user_id, entry_types, from_date, to_date, limit, cursor)

    def list_usage_entries(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage:
        """List usage entries with the canonical ledger cursor."""
        return self._store.list_usage_entries(user_id, from_date, to_date, limit, cursor)

    def list_usage_charges(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: UsageChargeCursor | None = None,
    ) -> UsageChargePage:
        """List metered usage charges, including allowance-covered events."""
        return self._store.list_usage_charges(user_id, from_date, to_date, limit, cursor)

    def get_ledger_entry(self, user_id: str, entry_id: str) -> LedgerEntry | None:
        """Return one ledger entry for a user account."""
        return self._store.get_ledger_entry(user_id, entry_id)

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats:
        """Aggregate statistics across all users in a time window."""
        return self._analytics.aggregate_stats(start, end)

    def close(self) -> None:
        """Release resources owned by the configured credit store."""
        self._store.close()
