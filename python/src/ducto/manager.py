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

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ducto.engine import PricingEngine
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.base import CapReachedError, CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    AggregateStatsRow,
    BalanceResult,
    CheckFeatureResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PricingConfigData,
    RefundResult,
    ReserveResult,
    SetupResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamDeductionResult,
    TopUserRow,
    TransactionRow,
)
from ducto.metrics import UsageMetrics


class InsufficientCreditsError(Exception):
    """Raised when a user does not have enough credits for an operation."""


class PricingNotLoadedError(Exception):
    """Raised when ``deduct()`` is called before pricing is loaded."""


#: Default ``low_balance`` threshold = this multiple of the engine's
#: ``min_balance`` (contract §6 / M18). Override via the ``CreditManager``
#: ``low_balance_threshold`` constructor argument.
DEFAULT_LOW_BALANCE_MULTIPLIER = Decimal(2)


class CreditManager:
    """Orchestrates credit operations: pricing -> atomic deduct.

    Args:
        store: A ``CreditStore`` adapter (HttpxSupabaseStore, PostgresStore, etc.).
        engine: An optional pre-configured ``PricingEngine``. If omitted,
            call ``load_pricing_from_store()`` or ``publish_pricing_from_dict()``
            before ``deduct()``.
        emitter: An optional ``CreditEventEmitter`` for lifecycle events.
        low_balance_threshold: Absolute balance at or below which a deduction
            that *crosses* the threshold emits ``credits.low_balance`` (contract
            §6 / M18). When ``None`` (the default), the threshold is derived as
            ``min_balance * DEFAULT_LOW_BALANCE_MULTIPLIER`` (= ``min_balance *
            2``) at deduct time, so it tracks the engine's configured floor. The
            alert is **edge-triggered**: it fires once, on the deduction that
            takes the balance from above the threshold to at-or-below it, not on
            every call near the threshold.
    """

    def __init__(
        self,
        store: CreditStore,
        engine: PricingEngine | None = None,
        emitter: CreditEventEmitter | None = None,
        low_balance_threshold: Decimal | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._emitter = emitter
        self._low_balance_threshold = low_balance_threshold

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
        """Resolve the configured low-balance threshold (contract §6 / M18).

        Uses the explicit constructor value when set, else derives it from the
        engine's ``min_balance`` (defaulting to ``Decimal(5)`` if no engine is
        loaded) times :data:`DEFAULT_LOW_BALANCE_MULTIPLIER`.
        """
        if self._low_balance_threshold is not None:
            return self._low_balance_threshold
        min_bal = self._engine.min_balance if self._engine else Decimal(5)
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
        return result

    # -- Plan management ------------------------------------------------

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

    def reserve_credits(
        self,
        user_id: str,
        amount: Decimal | int,
        operation_type: str = "usage",
        metadata: CreditMetadata | None = None,
        min_balance: Decimal | int | None = None,
    ) -> ReserveResult:
        """Reserve credits for an upcoming operation.

        If ``min_balance`` is not specified, the engine's configured minimum
        is used (defaults to ``Decimal(5)`` if no engine is loaded).
        """
        if min_balance is not None:
            actual = Decimal(min_balance)
        else:
            actual = self._engine.min_balance if self._engine else Decimal(5)
        return self._store.reserve_credits(user_id, Decimal(amount), operation_type, metadata, actual)

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

        # Edge-triggered low_balance (M18): emit only when THIS deduction crossed
        # the configured threshold (balance_before > threshold >= balance_after).
        threshold = self._resolve_low_balance_threshold()
        balance_before = result.balance_after + result.amount
        if balance_before > threshold >= result.balance_after:
            self._emit(
                "credits.low_balance",
                user_id,
                {
                    "balance": result.balance_after,
                    "threshold": threshold,
                },
            )

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
    ) -> DeductionResult:
        """Shortcut for fixed-cost batch jobs (roadmap gen, topic gen, etc.).

        Rejects an unknown / unconfigured ``job_name`` rather than silently
        charging 0 credits (L1): the engine returns ``None`` for an unknown job,
        which would otherwise become a "successful" free deduction.

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
        )
