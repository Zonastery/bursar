"""High-level credit manager.

Orchestrates the full credit lifecycle:
  calculate -> reserve -> deduct

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

from datetime import datetime
from typing import Any

from ducto.engine import PricingEngine
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.base import CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    AggregateStatsRow,
    BalanceResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    PricingConfigData,
    RefundResult,
    ReserveResult,
    SetupResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamDeductionResult,
    TopUserRow,
)
from ducto.metrics import UsageMetrics


class InsufficientCreditsError(Exception):
    """Raised when a user does not have enough credits for an operation."""


class PricingNotLoadedError(Exception):
    """Raised when ``deduct()`` is called before pricing is loaded."""


class CreditManager:
    """Orchestrates credit operations: pricing -> reserve -> deduct.

    Args:
        store: A ``CreditStore`` adapter (HttpxSupabaseStore, PostgresStore, etc.).
        engine: An optional pre-configured ``PricingEngine``. If omitted,
            call ``load_pricing_from_store()`` or ``publish_pricing_from_dict()``
            before ``deduct()``.
        emitter: An optional ``CreditEventEmitter`` for lifecycle events.
    """

    def __init__(
        self,
        store: CreditStore,
        engine: PricingEngine | None = None,
        emitter: CreditEventEmitter | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._emitter = emitter

    def _emit(self, type_: str, user_id: str, data: dict[str, Any] | None = None) -> None:
        """Emit a credit lifecycle event. No-op if no emitter is configured."""
        if self._emitter:
            self._emitter.emit(
                CreditEvent(
                    type=type_,
                    timestamp=datetime.now(),
                    user_id=user_id,
                    data=data,
                )
            )

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
        amount: int,
        tx_type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        """Add credits to a user's account."""
        result = self._store.add_credits(user_id, amount, tx_type, metadata, expires_at)
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

    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str = "usage",
        metadata: CreditMetadata | None = None,
        min_balance: int | None = None,
    ) -> ReserveResult:
        """Reserve credits for an upcoming operation.

        If ``min_balance`` is not specified, the engine's configured minimum
        is used (defaults to 5 if no engine is loaded).
        """
        actual = min_balance if min_balance is not None else (self._engine.min_balance if self._engine else 5)
        return self._store.reserve_credits(user_id, amount, operation_type, metadata, actual)

    def deduct(
        self,
        user_id: str,
        metrics: UsageMetrics,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        """Full deduction flow: calculate -> reserve -> deduct.

        Args:
            user_id: The user to charge.
            metrics: Usage metrics (model, tokens, tool calls, etc.).
            idempotency_key: Optional unique key for idempotent dedup.
            metadata: Extra metadata to attach to the transaction.

        Returns:
            ``DeductionResult`` with transaction details.

        Raises:
            PricingNotLoadedError: If pricing hasn't been loaded.
            InsufficientCreditsError: If the user lacks sufficient balance
                (including the min_balance floor).
        """
        if not self._engine:
            raise PricingNotLoadedError(
                "PricingEngine not loaded. Call publish_pricing_from_dict() or load_pricing_from_store() first."
            )

        # 1) Calculate cost
        breakdown = self._engine.calculate(metrics)
        # Truncate fractional credits (always rounds down — consumer-friendly pricing)
        cost = int(breakdown.total) if breakdown.total > 0 else 0

        # ── Plan allowance check ─────────────────────────────────────────
        # Consume free plan allowance before deducting from balance
        if cost > 0:
            allowance = self._store.check_allowance(user_id)
            if allowance.allowance_remaining > 0:
                consume = min(cost, allowance.allowance_remaining)
                self._store.increment_usage_window(user_id, allowance.plan_id, consume)
                cost -= consume

        if cost <= 0:
            # Fully covered by plan allowance — no balance deduction
            balance = self._store.get_balance(user_id)
            result = DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=0,
                balance_after=balance.balance,
                idempotent=False,
            )
            self._emit(
                "credits.deducted",
                user_id,
                {
                    "amount": 0,
                    "balance_after": balance.balance,
                    "plan_covered": True,
                },
            )
            return result

        # ── Spend cap check ────────────────────────────────────────────
        cap_result = self._store.check_spend_cap(
            user_id=user_id,
            model=metrics.model,
            amount=cost,
        )
        if cap_result.action == "deny":
            self._emit(
                "credits.cap_reached",
                user_id,
                {
                    "current_spend": cap_result.current_spend,
                    "limit": cap_result.cap_limit,
                    "model": cap_result.model,
                    "amount": cost,
                },
            )
            model_info = f" ({cap_result.model})" if cap_result.model else ""
            raise InsufficientCreditsError(
                f"Spend cap exceeded: {cap_result.current_spend}/{cap_result.cap_limit}{model_info}"
            )
        if cap_result.action in ("warn", "notify"):
            self._emit(
                "credits.cap_warning",
                user_id,
                {
                    "current_spend": cap_result.current_spend,
                    "limit": cap_result.cap_limit,
                    "model": cap_result.model,
                    "amount": cost,
                    "action": cap_result.action,
                },
            )

        # 2) Build transaction metadata (merge user-provided over defaults)
        base = {
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
            "model": metrics.model,
            "breakdown_total": breakdown.total,
        }
        if metrics.fixed_job:
            base["fixed_job"] = metrics.fixed_job
        if idempotency_key:
            base["idempotency_key"] = idempotency_key
        if metadata:
            base.update(metadata.model_dump(exclude_none=True))
        tx_meta = CreditMetadata(**base)

        # 3) Reserve
        reserve_result = self._store.reserve_credits(
            user_id=user_id,
            amount=cost,
            operation_type=metrics.fixed_job or "usage",
            metadata=tx_meta,
            min_balance=self._engine.min_balance,
        )

        if reserve_result.error:
            raise InsufficientCreditsError(
                f"Credit reservation failed: {reserve_result.error}. User={user_id}, requested={cost}"
            )

        # 4) Deduct
        deduction = self._store.deduct_credits(
            user_id=user_id,
            reservation_id=reserve_result.reservation_id,
            amount=cost,
            idempotency_key=idempotency_key,
            metadata=tx_meta,
        )

        if deduction.error:
            raise InsufficientCreditsError(
                f"Credit deduction failed: {deduction.error}. User={user_id}, requested={cost}"
            )

        result = DeductionResult(
            transaction_id=deduction.transaction_id,
            user_id=deduction.user_id,
            amount=deduction.amount,
            balance_after=deduction.balance_after,
            idempotent=deduction.idempotent,
        )

        self._emit(
            "credits.deducted",
            user_id,
            {
                "transaction_id": result.transaction_id,
                "amount": result.amount,
                "balance_after": result.balance_after,
                "model": metrics.model,
            },
        )

        # Emit low_balance when balance after deduct is at or below min_balance * 2
        min_bal = self._engine.min_balance if self._engine else 5
        if result.balance_after <= min_bal * 2:
            self._emit(
                "credits.low_balance",
                user_id,
                {
                    "balance": result.balance_after,
                    "min_balance": min_bal,
                },
            )

        return result

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
            ``RefundResult`` with the refund transaction details.
        """
        result = self._store.refund_credits(transaction_id, amount, reason, metadata)
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
        cost = int(breakdown.total) if breakdown.total > 0 else 0

        if cost <= 0:
            team_bal = self._store.get_team_balance(team_id)
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=0,
                team_balance_after=team_bal.balance,
            )

        result = self._store.deduct_team(team_id, user_id, cost, metadata)
        if not result.error:
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
        """Shortcut for fixed-cost batch jobs (roadmap gen, topic gen, etc.)."""
        return self.deduct(
            user_id=user_id,
            metrics=UsageMetrics(fixed_job=job_name),
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
