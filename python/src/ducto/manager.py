"""High-level credit manager.

Orchestrates the credit lifecycle. The hot "calculate cost then charge now"
path is a single atomic, idempotency-keyed store transaction
(``deduct_with_allowance``) — allowance, spend cap, balance floor and debit all
commit (or roll back) together inside the store (contract §2, C1).

Example::

    from ducto import CreditManager, UsageMetrics
    from ducto.interface.supabase import HttpxSupabaseStore

    store = HttpxSupabaseStore(url=supabase_url, key=service_role_key)
    manager = CreditManager(store=store)

    # One-time setup (creates tables + RPCs)
    manager.setup()

    # Load pricing from store (credit_pricing_config table)
    manager.load_pricing_from_store()

    # Deduct credits for a usage event
    result = manager.deduct(
        user_id="user_abc",
        metrics=UsageMetrics(model="claude-opus-4", input_tokens=500, output_tokens=200),
        idempotency_key="chat_42_turn_7",
    )
    print(f"Deducted {result.amount} credits, balance: {result.balance_after}")
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ducto.allowance import resolve_allowance_window
from ducto.engine import PricingEngine
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.base import CapReachedError, CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    AggregateStatsRow,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BillingMode,
    CanAffordResult,
    CheckFeatureResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    LeaseResult,
    OperationPolicy,
    PricingConfigData,
    RefundResult,
    ReleaseResult,
    SetupResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamDeductionResult,
    TopUserRow,
    TransactionRow,
)
from ducto.metrics import UsageMetrics


class CreditError(Exception):
    """Coherent base for ducto credit-domain errors raised by the manager.

    Lets callers ``except CreditError`` to catch any admission/settle failure
    (interface plan §3 / M2), while still distinguishing specific subclasses.
    """


class InsufficientCreditsError(CreditError):
    """Raised when a user does not have enough credits for an operation."""


class PricingNotLoadedError(CreditError):
    """Raised when ``deduct()`` is called before pricing is loaded."""


class ConcurrencyLimitError(CreditError):
    """Raised when a ``reserve`` would exceed an operation's ``max_concurrent`` leases."""


class FeatureNotEntitledError(CreditError):
    """Raised when an operation requires a plan feature the user does not have."""


class LeaseExpiredError(CreditError):
    """Raised when settling/renewing a lease whose TTL has already elapsed."""


class LeaseNotFoundError(CreditError):
    """Raised when a lease id does not exist, belongs to another user, or was released."""


logger = logging.getLogger(__name__)

#: Default ``low_balance`` threshold = this multiple of the engine's
#: ``min_balance`` (contract §6 / M18). Override via the ``CreditManager``
#: ``low_balance_threshold`` constructor argument.
DEFAULT_LOW_BALANCE_MULTIPLIER = Decimal(2)

#: Default lease TTL (seconds) for ``reserve``/``runBilled`` (interface plan §3).
#: Long batch/agentic jobs call :meth:`CreditManager.renew` before this elapses.
DEFAULT_LEASE_TTL_SECONDS = 600

#: Built-in financial-safety presets (interface plan §2). ``strict_prepaid`` keeps
#: the floor ``>= 0`` (structural zero debt); ``overdraft`` permits a negative floor
#: and bills the full actual cost at settle.
POLICY_PRESETS = frozenset({"strict_prepaid", "overdraft"})


@dataclass
class LowBalanceConfig:
    """Configuration for the ``credits.low_balance`` signal (interface plan §6 / WS7).

    Collapses the previous three overlapping ``CreditManager`` constructor
    params (``low_balance_threshold``, ``low_balance_thresholds``,
    ``on_low_balance``) into one object.

    Args:
        thresholds: Absolute balance levels at/below which a deduction that
            *crosses* a level emits ``credits.low_balance``. A single-element
            list behaves like the old single-threshold form; multiple elements
            fire once per level per descent (edge-triggered, high→low), and
            each level re-arms independently once the balance climbs back
            above it (e.g. via ``add_credits``). When ``None`` (the default
            when no ``LowBalanceConfig`` is passed at all), the threshold is
            derived lazily as ``min_balance * DEFAULT_LOW_BALANCE_MULTIPLIER``
            at deduct time, so it tracks the engine's configured floor.
        on_trigger: Optional non-blocking callback invoked (in addition to the
            ``credits.low_balance`` event) whenever a level fires. Exceptions
            raised by the callback are logged and never propagate (H4).
    """

    thresholds: list[Decimal] | None = None
    on_trigger: Callable[[CreditEvent], None] | None = None


class CreditManager:
    """Orchestrates credit operations: pricing -> atomic deduct.

    Args:
        store: A ``CreditStore`` adapter (HttpxSupabaseStore, PostgresStore, etc.).
        engine: An optional pre-configured ``PricingEngine``. If omitted,
            call ``load_pricing_from_store()`` or ``publish_pricing_from_dict()``
            before ``deduct()``.
        emitter: An optional ``CreditEventEmitter`` for lifecycle events.
        low_balance: An optional :class:`LowBalanceConfig` configuring the
            ``credits.low_balance`` signal (contract §6 / M18 / WS7). When
            ``None`` (the default), no explicit thresholds are configured and
            the threshold is derived lazily from ``min_balance`` at deduct
            time (see :class:`LowBalanceConfig`).
    """

    def __init__(
        self,
        store: CreditStore,
        engine: PricingEngine | None = None,
        emitter: CreditEventEmitter | None = None,
        *,
        policy: str = "strict_prepaid",
        overdraft_floor: Decimal | None = None,
        max_concurrent: int | None = None,
        low_balance: LowBalanceConfig | None = None,
        default_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        if policy not in POLICY_PRESETS:
            raise ValueError(f"unknown policy preset {policy!r}; expected one of {sorted(POLICY_PRESETS)}")
        self._store = store
        self._engine = engine
        self._emitter = emitter
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
        self._lb_below: dict[str, set[Decimal]] = {}
        self._lb_lock = threading.RLock()

    def _emit(self, type_: str, user_id: str, data: dict[str, Any] | None = None) -> None:
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

    def _resolve_low_balance_threshold(self) -> Decimal:
        """Resolve the low-balance threshold when no explicit thresholds are
        configured (contract §6 / M18 / WS7): derived lazily from the engine's
        ``min_balance`` (defaulting to ``Decimal(0)`` if no engine is loaded)
        times :data:`DEFAULT_LOW_BALANCE_MULTIPLIER`.
        """
        min_bal = self._engine.min_balance if self._engine else Decimal(0)
        return min_bal * DEFAULT_LOW_BALANCE_MULTIPLIER

    # -- Schema management -----------------------------------------------

    def setup(self) -> SetupResult:
        """Run bundled SQL migrations through the store."""
        return self._store.setup()

    # -- Pricing configuration -------------------------------------------

    def publish_pricing_from_dict(self, data: PricingConfigData | dict[str, Any]) -> None:
        """Load pricing from a ``PricingConfigData`` or raw dict and sync it."""
        raw = data if isinstance(data, dict) else data.model_dump(exclude_none=True)
        engine = PricingEngine.from_dict(raw)
        self._engine = engine
        config = data if isinstance(data, PricingConfigData) else PricingConfigData.model_validate(data)
        self._store.set_active_pricing(config)

    def load_pricing_from_store(self) -> None:
        """Load the active pricing config from the store."""
        active = self._store.get_active_pricing()
        if active is None:
            raise PricingNotLoadedError(
                "No active pricing config found in the store. "
                "Call publish_pricing_from_dict() or set_active_pricing() first."
            )
        engine_dict = active.config.model_dump(exclude_none=True)
        self._engine = PricingEngine.from_dict(engine_dict)

    def publish_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> None:
        """Publish new pricing and update the engine in one call."""
        raw = config.model_dump(exclude_none=True)
        self._engine = PricingEngine.from_dict(raw)
        self._store.set_active_pricing(config, label=label)

    @property
    def engine(self) -> PricingEngine | None:
        """The current PricingEngine, or None if not loaded."""
        return self._engine

    # -- Credit operations -----------------------------------------------

    def get_balance(self, user_id: str) -> BalanceResult:
        """Get a user's current credit balance."""
        return self._store.get_balance(user_id)

    def add_credits(
        self,
        user_id: str,
        amount: Decimal | int,
        tx_type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        """Add credits to a user's account (``amount`` is a ``Decimal``)."""
        result = self._store.add_credits(user_id, Decimal(amount), tx_type, metadata, expires_at)
        self._emit(
            "credits.added",
            user_id,
            {
                "transaction_id": result.transaction_id,
                "amount": result.amount,
                "new_balance": result.new_balance,
                "type": tx_type,
            },
        )
        # Re-arm multi-level low_balance: any level the topped-up balance is now back
        # above can fire again on the next descent (interface plan §6).
        if self._low_balance_thresholds:
            with self._lb_lock:
                below = self._lb_below.setdefault(user_id, set())
                for t in self._low_balance_thresholds:
                    if result.new_balance > t:
                        below.discard(t)
        return result

    # -- Plan management ------------------------------------------------

    def set_user_plan(self, user_id: str, plan_key: str) -> SetUserPlanResult:
        """Assign a plan to a user and emit a ``credits.plan_changed`` event.

        Args:
            user_id: The user to assign the plan to.
            plan_key: The plan key to assign (e.g. ``"pro"``).

        Returns:
            ``SetUserPlanResult`` confirming the assignment.
        """
        result = self._store.set_user_plan(user_id, plan_key)
        self._emit(
            "credits.plan_changed",
            user_id,
            {
                "user_id": user_id,
                "plan_key": plan_key,
                "timestamp": datetime.now(UTC),
            },
        )
        return result

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        """Fetch user's current plan (including feature entitlements)."""
        return self._store.get_user_plan(user_id)

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
        """Resolve the effective policy: explicit arg → per-op → plan → preset (§1).

        A **planless** user (``plan_id`` is ``None``) always gets the constructor
        preset, never silently unlimited (resolves M1). A user *with* a plan gets
        the plan default, then any ``per_operation`` override, then the explicit
        per-call ``billing_mode``.
        """
        policy = self._preset_policy()

        # Intentionally not catching exceptions: a store outage at plan-fetch time
        # must surface to the caller rather than silently demoting the user to the
        # constructor preset (which can flip a paid/overdraft user to strict_prepaid
        # and block legitimate requests without any signal — Fix 4).
        plan = self._store.get_user_plan(user_id)

        if plan is not None and plan.plan_id:
            policy = OperationPolicy(
                billing_mode=plan.default_billing_mode,
                max_concurrent=plan.max_concurrent if plan.max_concurrent is not None else policy.max_concurrent,
                overdraft_floor=plan.overdraft_floor if plan.overdraft_floor is not None else policy.overdraft_floor,
            )
            op = (plan.per_operation or {}).get(operation_type)
            if op is not None:
                policy = OperationPolicy(
                    billing_mode=op.billing_mode,
                    max_concurrent=op.max_concurrent if op.max_concurrent is not None else policy.max_concurrent,
                    overdraft_floor=op.overdraft_floor if op.overdraft_floor is not None else policy.overdraft_floor,
                )

        if billing_mode_override is not None:
            policy = policy.model_copy(update={"billing_mode": billing_mode_override})
        return policy

    def _resolve_floor(self, policy: OperationPolicy) -> Decimal:
        """Admission floor for a policy: ``overdraft_floor`` (≤0) or ``min_balance`` (≥0)."""
        if policy.billing_mode == "overdraft":
            return policy.overdraft_floor if policy.overdraft_floor is not None else Decimal(0)
        return self._engine.min_balance if self._engine else Decimal(0)

    def _resolve_allowance_period_start(self, user_id: str) -> date | None:
        """Resolve the allowance-window ``period_start`` for a user (WS9).

        Fast path: a ``calendar_month`` plan (the default, and the pre-WS9
        behavior) returns ``None`` so the store/SQL default (calendar-month via
        ``date_trunc('month', now())``) applies with no extra computation —
        this keeps existing calendar_month behavior byte-for-byte unchanged.

        For ``rolling_30d``/``anniversary`` plans, resolves the window via
        :func:`resolve_allowance_window` anchored on ``plan_assigned_at`` and
        returns just the ``period_start`` date.
        """
        plan = self._store.get_user_plan(user_id)
        if plan.allowance_period == "calendar_month":
            return None
        period_start, _period_end = resolve_allowance_window(
            datetime.now(UTC), plan.allowance_period, plan.plan_assigned_at
        )
        return period_start

    def _cost_of(self, metrics_or_amount: UsageMetrics | Decimal | int) -> tuple[Decimal, str | None]:
        """Compute the credit cost and model from metrics, or pass a raw amount.

        For :class:`UsageMetrics` the cost is ``engine.calculate(...).total`` (exact
        ``Decimal``, no truncation); a raw amount is used as-is with no model.
        """
        if isinstance(metrics_or_amount, UsageMetrics):
            if not self._engine:
                raise PricingNotLoadedError(
                    "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
                )
            breakdown = self._engine.calculate(metrics_or_amount)
            return breakdown.total, metrics_or_amount.model
        return Decimal(metrics_or_amount), None

    def _raise_lease_error(self, error: str, user_id: str, amount: Decimal) -> None:
        """Map a store business code to the coherent typed exception (M2)."""
        if error == "concurrency_limit":
            raise ConcurrencyLimitError(f"Concurrency limit reached. User={user_id}")
        if error == "cap_reached":
            raise CapReachedError(f"Spend cap exceeded. User={user_id}, requested={amount}")
        if error == "feature_not_entitled":
            raise FeatureNotEntitledError(f"Feature not entitled. User={user_id}")
        if error == "insufficient_credits":
            raise InsufficientCreditsError(f"Insufficient credits. User={user_id}, requested={amount}")
        if error == "lease_expired":
            raise LeaseExpiredError(f"Lease expired. User={user_id}")
        if error in ("lease_not_found", "not_found"):
            raise LeaseNotFoundError(f"Lease not found. User={user_id}")
        if error == "invalid_amount":
            raise ValueError(f"Invalid amount: {amount}")
        raise CreditError(f"Operation failed: {error}. User={user_id}")

    def reserve(
        self,
        user_id: str,
        metrics_or_amount: UsageMetrics | Decimal | int,
        *,
        operation_type: str = "usage",
        billing_mode: BillingMode | None = None,
        required_feature: str | None = None,
        ttl: int | None = None,
        metadata: CreditMetadata | None = None,
        model: str | None = None,
    ) -> LeaseResult:
        """Atomically acquire a lease — the only admission control (D4).

        Resolves the effective policy, enforces ``required_feature``, sizes the hold
        from ``metrics_or_amount`` (worst-case in strict, estimate in overdraft — the
        caller chooses what to pass), and calls the store's atomic ``create_lease``.

        The store's ``create_lease`` is allowance-aware: remaining free allowance is
        added to the effective headroom so free-tier users are not falsely rejected
        for worst-case holds they can cover with allowance (Fix 1 / D4).

        ``model`` is inferred from ``UsageMetrics`` when passed; for raw
        ``Decimal``/``int`` amounts use the explicit ``model`` kwarg so per-model
        spend-caps and analytics remain accurate (Fix 5).

        On any business failure raises the coherent typed exception; on success emits
        ``credits.reserved`` and returns the :class:`LeaseResult`.
        """
        if required_feature is not None:
            check = self._store.check_feature(user_id, required_feature)
            if not check.has_feature:
                raise FeatureNotEntitledError(f"Feature {required_feature!r} not entitled. User={user_id}")

        policy = self._resolve_policy(user_id, operation_type, billing_mode)
        floor = self._resolve_floor(policy)
        amount, derived_model = self._cost_of(metrics_or_amount)
        # When caller passes a raw Decimal/int (no model in metrics), fall back to
        # the explicit ``model`` kwarg so cap checks and analytics are not blind.
        effective_model = derived_model if derived_model is not None else model
        ttl_seconds = ttl if ttl is not None else self._default_ttl
        period_start = self._resolve_allowance_period_start(user_id)

        result = self._store.create_lease(
            user_id,
            amount,
            operation_type,
            billing_mode=policy.billing_mode,
            floor=floor,
            max_concurrent=policy.max_concurrent,
            ttl_seconds=ttl_seconds,
            model=effective_model,
            overdraft_floor=policy.overdraft_floor,
            metadata=metadata,
            period_start=period_start,
        )

        if result.error:
            self._emit(
                "credits.deduct_failed",
                user_id,
                {"error": result.error, "amount": amount, "stage": "reserve", "operation_type": operation_type},
            )
            self._raise_lease_error(result.error, user_id, amount)

        self._emit(
            "credits.reserved",
            user_id,
            {
                "lease_id": result.lease_id,
                "amount": result.amount,
                "available": result.available,
                "billing_mode": result.billing_mode,
                "operation_type": operation_type,
                "expires_at": result.expires_at,
            },
        )
        return result

    def settle(
        self,
        user_id: str,
        lease_id: str,
        metrics_or_amount: UsageMetrics | Decimal | int,
        *,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
        skip_allowance: bool = False,
    ) -> DeductionResult:
        """Charge the ACTUAL cost against a lease and finalize it (D5).

        De-clamped: bills the full actual cost even if it exceeds the lease hold
        (overdraft). Never blocks on floor/cap at settle — a cap breach surfaces as a
        non-blocking ``credits.cap_warning``/``credits.cap_reached`` signal. Emits
        ``credits.deducted``, then multi-level ``credits.low_balance`` and a
        ``credits.overdraft`` signal if the balance went negative.

        ``skip_allowance=True`` prevents the free inference allowance from being
        consumed at settle time. Use for fixed-cost operations reserved via the
        lease pattern (mirrors the ``deduct_fixed`` / ``deduct`` ``skip_allowance``
        flag — Fix 7 / #4).
        """
        amount, model = self._cost_of(metrics_or_amount)

        if isinstance(metrics_or_amount, UsageMetrics):
            tx_meta = self._build_tx_metadata(metrics_or_amount, amount, idempotency_key, metadata)
        else:
            base: dict[str, Any] = metadata.model_dump(exclude_none=True) if metadata else {}
            if idempotency_key:
                base["idempotency_key"] = idempotency_key
            tx_meta = CreditMetadata(**base)

        result = self._store.settle_lease(
            user_id,
            lease_id,
            amount,
            idempotency_key=idempotency_key,
            min_balance=self._engine.min_balance if self._engine else Decimal(0),
            model=model,
            metadata=tx_meta,
            skip_allowance=skip_allowance,
            period_start=self._resolve_allowance_period_start(user_id),
        )

        if result.error:
            self._emit(
                "credits.deduct_failed",
                user_id,
                {"error": result.error, "amount": amount, "stage": "settle", "lease_id": lease_id},
            )
            if result.error == "lease_expired":
                self._emit("credits.lease_expired", user_id, {"lease_id": lease_id})
            self._raise_lease_error(result.error, user_id, amount)

        self._emit(
            "credits.deducted",
            user_id,
            {
                "transaction_id": result.transaction_id,
                "amount": result.amount,
                "allowance_consumed": result.allowance_consumed,
                "balance_after": result.balance_after,
                "model": model,
                "lease_id": lease_id,
                "idempotent": result.idempotent,
            },
        )

        # Cap signal: 'deny' breaching at settle is non-blocking (work is done) and
        # re-emitted as cap_reached; warn/notify as cap_warning (interface plan §7).
        if result.cap_warning == "deny":
            self._emit("credits.cap_reached", user_id, {"amount": result.amount, "model": model, "blocking": False})
        elif result.cap_warning in ("warn", "notify"):
            self._emit(
                "credits.cap_warning",
                user_id,
                {
                    "balance_after": result.balance_after,
                    "amount": result.amount,
                    "model": model,
                    "action": result.cap_warning,
                },
            )

        self._post_charge_signals(user_id, result)
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
        """Extend a lease's TTL for long batch/agentic jobs (B4)."""
        ttl_seconds = ttl if ttl is not None else self._default_ttl
        result = self._store.renew_lease(user_id, lease_id, ttl_seconds)
        if result.error:
            if result.error == "lease_expired":
                self._emit("credits.lease_expired", user_id, {"lease_id": lease_id})
            self._raise_lease_error(result.error, user_id, Decimal(0))
        return result

    def can_afford(
        self,
        user_id: str,
        metrics_or_amount: UsageMetrics | Decimal | int,
        *,
        required_feature: str | None = None,
        billing_mode: BillingMode | None = None,
        operation_type: str = "usage",
    ) -> CanAffordResult:
        """Advisory affordability check — UI only, non-locking, may be stale (D4/H3).

        ``spendable`` in the result reflects the user's effective spending power:
        ``balance − active holds + allowance_remaining``. This matches the headroom
        ``reserve`` uses so the Send-button check agrees with the admission gate
        (Fix 1). Never use this as an admission gate; only ``reserve`` is authoritative.
        """
        worst_case, _ = self._cost_of(metrics_or_amount)
        avail = self._store.get_available(user_id)

        # can_afford() is an advisory / UI method — it must never raise (#7).
        # _resolve_policy may call store.get_user_plan; wrap it so a transient
        # store outage returns a cautious affordable=False rather than an exception.
        try:
            policy = self._resolve_policy(user_id, operation_type, billing_mode)
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
        except Exception:
            pass  # advisory check: fail open if allowance fetch fails

        spendable = avail.available + allowance_credit

        affordable = True
        reason: str | None = None
        if required_feature is not None:
            check = self._store.check_feature(user_id, required_feature)
            if not check.has_feature:
                affordable = False
                reason = "feature_not_entitled"
        if affordable and (spendable - worst_case) < floor:
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

    def check_allowance(self, user_id: str) -> AllowanceResult:
        """Get remaining free allowance for the current billing period (Fix 6).

        Convenience wrapper that routes through the manager so callers never need
        to reach past it into the raw store. Returns a zero-allowance result for
        planless users (no exception).

        For ``rolling_30d``/``anniversary`` plans (WS9), resolves and threads
        ``period_start`` so the reported window matches the one actually used by
        ``deduct``/``settle``/``reserve`` — a ``calendar_month`` plan (the
        default) takes the fast path unchanged.
        """
        plan = self._store.get_user_plan(user_id)
        if not plan.plan_id or plan.allowance_period == "calendar_month":
            return self._store.check_allowance(user_id)
        period_start, period_end_exclusive = resolve_allowance_window(
            datetime.now(UTC), plan.allowance_period, plan.plan_assigned_at
        )
        result = self._store.check_allowance(user_id, period_start=period_start)
        # The store/SQL layer only knows a generic calendar-month period_end
        # (see 022_configurable_allowance_window.sql); override it here with the
        # authoritative window this manager resolved.
        period_end_inclusive = period_end_exclusive - timedelta(days=1)
        period_start_dt = datetime(period_start.year, period_start.month, period_start.day, tzinfo=UTC)
        return result.model_copy(
            update={
                "period_start": period_start_dt.isoformat(),
                "period_end": datetime(
                    period_end_inclusive.year,
                    period_end_inclusive.month,
                    period_end_inclusive.day,
                    tzinfo=UTC,
                ).isoformat(),
            }
        )

    def run_billed(
        self,
        user_id: str,
        *,
        estimate: UsageMetrics | Decimal | int,
        do_work: Callable[[], tuple[Any, UsageMetrics | Decimal | int]],
        operation_type: str = "usage",
        billing_mode: BillingMode | None = None,
        required_feature: str | None = None,
        idempotency_key: str | None = None,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        """One-call shortcut wiring reserve → do_work → settle (interface plan §4).

        ``do_work`` runs the operation and returns ``(result, actual)`` where
        ``actual`` is the real usage metrics (or amount) to settle. On any exception
        from ``do_work`` the lease is released and the error re-raised. For long jobs
        ``do_work`` may call :meth:`renew`. A crash between reserve and settle is
        covered by the lease TTL (and the store's reaper).
        """
        lease = self.reserve(
            user_id,
            estimate,
            operation_type=operation_type,
            billing_mode=billing_mode,
            required_feature=required_feature,
            ttl=ttl,
        )
        try:
            work_result, actual = do_work()
        except Exception:
            self.release(user_id, lease.lease_id)
            raise

        deduction = self.settle(user_id, lease.lease_id, actual, idempotency_key=idempotency_key)
        return {"result": work_result, "deduction": deduction}

    # ── Low-balance / overdraft signals (interface plan §6) ─────────────

    def _post_charge_signals(self, user_id: str, result: DeductionResult) -> None:
        """Emit overdraft, floor-breach, and multi-level low_balance after a charge.

        Overdraft (balance < 0) is always signalled.  Floor breach (0 ≤ balance <
        min_balance) is a non-blocking signal for strict-mode users: the work is
        already done but the operator should know the balance slipped below the
        configured floor, which means a prior hold was under-sized (Fix 2).

        Idempotent replays are skipped entirely at the top: re-emitting overdraft
        or floor_breach with the *original* balance figures against the *current*
        live balance would produce spurious duplicate events (Fix 2/#2).
        """
        if result.idempotent:
            return

        if result.balance_after < 0:
            self._emit("credits.overdraft", user_id, {"balance": result.balance_after, "amount": result.amount})
        else:
            # Emit a non-blocking floor breach when balance slipped below min_balance
            # without going negative (strict-mode under-estimate of worst-case cost).
            min_bal = self._engine.min_balance if self._engine else Decimal(0)
            if min_bal > 0 and result.balance_after < min_bal:
                self._emit(
                    "credits.floor_breach",
                    user_id,
                    {"balance": result.balance_after, "min_balance": min_bal, "amount": result.amount},
                )

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
                below = self._lb_below.setdefault(user_id, set())
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
                self._on_low_balance(event)
            except Exception:  # never block/break the op on a handler failure (§6/H4)
                logger.exception("on_low_balance handler failed for user %s", user_id)

    def _build_tx_metadata(
        self,
        metrics: UsageMetrics,
        breakdown_total: Decimal,
        idempotency_key: str | None,
        metadata: CreditMetadata | None,
    ) -> CreditMetadata:
        """Build transaction metadata: caller fields first, system fields last.

        System-owned keys (``idempotency_key``, ``model``, ``breakdown_total``)
        are applied after caller metadata so they always win (contract §5, M7).
        """
        base: dict[str, Any] = {}
        # Caller metadata first — system fields below overwrite any collisions.
        if metadata:
            base.update(metadata.model_dump(exclude_none=True))
        # System fields last (M7): these must not be overwritten by the caller.
        base["input_tokens"] = metrics.input_tokens
        base["output_tokens"] = metrics.output_tokens
        base["model"] = metrics.model
        base["breakdown_total"] = breakdown_total
        if metrics.fixed_job:
            base["fixed_job"] = metrics.fixed_job
        if idempotency_key:
            base["idempotency_key"] = idempotency_key
        return CreditMetadata(**base)

    def deduct(
        self,
        user_id: str,
        metrics: UsageMetrics,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
        *,
        skip_allowance: bool = False,
    ) -> DeductionResult:
        """Calculate the cost and charge it in one atomic store transaction.

        The flow is thin: ``breakdown = engine.calculate(metrics)`` →
        ``cost = breakdown.total`` (a ``Decimal``, charged exactly with **no**
        truncation) → if ``cost <= 0`` short-circuit with a zero-amount result →
        otherwise ``store.deduct_with_allowance(...)``. Allowance consumption,
        spend-cap enforcement, the balance floor, and the debit all commit (or
        roll back) together inside the store (contract §2, C1). The manager only
        maps the returned ``error`` code to a typed exception and emits events.

        Args:
            user_id: The user to charge.
            metrics: Usage metrics (model, tokens, tool calls, etc.).
            idempotency_key: Optional user-scoped key for idempotent replay.
            metadata: Extra metadata to attach to the transaction.
            skip_allowance: When ``True``, bypass free-allowance consumption so
                the full cost is charged to the balance. Pass ``True`` for
                fixed-cost batch jobs (via ``deduct_fixed``) to keep inference
                allowance uncontaminated (Fix 7).

        Returns:
            ``DeductionResult`` whose ``amount`` is the net (positive) charge to
            the balance after free allowance.

        Raises:
            PricingNotLoadedError: If pricing hasn't been loaded.
            InsufficientCreditsError: If the balance floor would be breached.
            CapReachedError: If a ``deny`` spend cap would be exceeded.
        """
        if not self._engine:
            raise PricingNotLoadedError(
                "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
            )

        # 1) Calculate cost — exact Decimal, NO truncation (H1).
        breakdown = self._engine.calculate(metrics)
        cost = breakdown.total

        # 2) Short-circuit a zero (or non-positive) cost: nothing to charge.
        if cost <= 0:
            balance = self._store.get_balance(user_id)
            result = DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=balance.balance,
                idempotent=False,
            )
            self._emit(
                "credits.deducted",
                user_id,
                {
                    "amount": Decimal(0),
                    "balance_after": balance.balance,
                    "plan_covered": True,
                },
            )
            return result

        # 3) One atomic transaction in the store: allowance → cap → floor → debit.
        tx_meta = self._build_tx_metadata(metrics, breakdown.total, idempotency_key, metadata)
        result = self._store.deduct_with_allowance(
            user_id,
            cost,
            idempotency_key=idempotency_key,
            min_balance=self._engine.min_balance,
            model=metrics.model,
            metadata=tx_meta,
            skip_allowance=skip_allowance,
            period_start=self._resolve_allowance_period_start(user_id),
        )

        # 4) Error path: emit a failure event and raise the typed exception.
        #    Never emit a success event here.
        if result.error:
            self._emit(
                "credits.deduct_failed",
                user_id,
                {
                    "error": result.error,
                    "amount": cost,
                    "model": metrics.model,
                },
            )
            if result.error == "cap_reached":
                self._emit(
                    "credits.cap_reached",
                    user_id,
                    {
                        "amount": cost,
                        "model": metrics.model,
                    },
                )
                raise CapReachedError(f"Spend cap exceeded. User={user_id}, requested={cost}")
            if result.error == "insufficient_credits":
                raise InsufficientCreditsError(f"Insufficient credits. User={user_id}, requested={cost}")
            # Any other business code (e.g. invalid_amount): surface it generically.
            raise InsufficientCreditsError(f"Deduction failed: {result.error}. User={user_id}, requested={cost}")

        # 5) Success path.
        self._emit(
            "credits.deducted",
            user_id,
            {
                "transaction_id": result.transaction_id,
                "amount": result.amount,
                "allowance_consumed": result.allowance_consumed,
                "balance_after": result.balance_after,
                "model": metrics.model,
            },
        )

        # Non-blocking spend-cap signal surfaced by the store.
        if result.cap_warning in ("warn", "notify"):
            self._emit(
                "credits.cap_warning",
                user_id,
                {
                    "balance_after": result.balance_after,
                    "amount": result.amount,
                    "model": metrics.model,
                    "action": result.cap_warning,
                },
            )

        # Edge-triggered low_balance (M18): multi-level if configured (WS7), else
        # single-threshold — see _emit_low_balance for the shared logic used by
        # both the direct-deduct path and the lease/settle path.
        balance_before = result.balance_after + result.amount
        self._emit_low_balance(user_id, balance_before, result.balance_after)

        return result

    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | int | None = None,
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
            ``RefundResult`` with the refund transaction details. On a business
            failure (over-refund, duplicate, wrong type, not found) ``error`` is
            set, ``credits.refund_failed`` is emitted, and **no**
            ``credits.refunded`` event fires (contract §4, H3). Inspect
            ``result.error`` (codes: ``over_refund``, ``already_refunded``,
            ``not_found``) to handle the failure.
        """
        refund_amount = Decimal(amount) if amount is not None else None
        result = self._store.refund_credits(transaction_id, refund_amount, reason, metadata)

        # Check the error BEFORE emitting (H3): a failed/duplicate/over-refund
        # must never fire a success event.
        if result.error:
            self._emit(
                "credits.refund_failed",
                result.user_id,
                {
                    "transaction_id": transaction_id,
                    "error": result.error,
                    "reason": reason,
                },
            )
            return result

        self._emit(
            "credits.refunded",
            result.user_id,
            {
                "transaction_id": transaction_id,
                "refund_transaction_id": result.refund_transaction_id,
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
            ``TeamDeductionResult`` with transaction details.
        """
        if not self._engine:
            raise PricingNotLoadedError(
                "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
            )

        breakdown = self._engine.calculate(metrics)
        cost = breakdown.total  # exact Decimal, no truncation (H1)

        if cost <= 0:
            team_bal = self._store.get_team_balance(team_id)
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=Decimal(0),
                team_balance_after=team_bal.balance,
            )

        result = self._store.deduct_team(
            team_id,
            user_id,
            cost,
            metadata,
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
            raise InsufficientCreditsError(
                f"Team deduction failed: {result.error}. Team={team_id}, user={user_id}, requested={cost}"
            )

        self._emit(
            "credits.deducted",
            user_id,
            {
                "transaction_id": result.transaction_id,
                "amount": result.amount,
                "team_balance_after": result.team_balance_after,
                "team_id": team_id,
                "deduct_type": "team",
            },
        )
        return result

    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult:
        """Sweep expired credits from all users' balances.

        Args:
            dry_run: If True, report without modifying.

        Returns:
            ``SweepResult`` with expired count and amount.
        """
        result = self._store.sweep_expired_credits(dry_run)
        if not dry_run and result.expired_count > 0:
            self._emit(
                "credits.expired",
                "system",
                {
                    "expired_count": result.expired_count,
                    "expired_amount": result.expired_amount,
                },
            )
        return result

    # ── Usage analytics ─────────────────────────────────────────────────

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Aggregate spend by user in a time window."""
        return self._store.spend_by_user(start, end)

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Aggregate spend by model in a time window."""
        return self._store.spend_by_model(start, end)

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        """Top users by spend in a time window."""
        return self._store.top_users(limit, start, end)

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        """Daily spend aggregation in a time window."""
        return self._store.daily_spend(start, end)

    def list_user_transactions(
        self,
        user_id: str,
        types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TransactionRow]:
        """List credit transactions for a user with pagination."""
        return self._store.list_user_transactions(user_id, types, from_date, to_date, limit, offset)

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        """Aggregate statistics across all users in a time window."""
        return self._store.aggregate_stats(start, end)

    def deduct_fixed(
        self,
        user_id: str,
        job_name: str,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
        *,
        use_allowance: bool = False,
    ) -> DeductionResult:
        """Shortcut for fixed-cost batch jobs (roadmap gen, topic gen, etc.).

        Rejects an unknown / unconfigured ``job_name`` rather than silently
        charging 0 credits (L1): the engine returns ``None`` for an unknown job,
        which would otherwise become a "successful" free deduction.

        ``use_allowance`` defaults to ``False``: fixed-cost operations (PDF
        generation, training runs, …) and monthly free inference allowances are
        separate budgets and must not cross-contaminate. Pass
        ``use_allowance=True`` to bill fixed-cost jobs against the allowance
        pool first instead.

        Raises:
            PricingNotLoadedError: If pricing hasn't been loaded.
            ValueError: If ``job_name`` is not a configured fixed-cost job.
        """
        if not self._engine:
            raise PricingNotLoadedError(
                "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
            )
        if self._engine.get_fixed_cost(job_name) is None:
            raise ValueError(f"Unknown fixed-cost job: {job_name!r}")

        return self.deduct(
            user_id=user_id,
            metrics=UsageMetrics(fixed_job=job_name),
            idempotency_key=idempotency_key,
            metadata=metadata,
            skip_allowance=not use_allowance,
        )
