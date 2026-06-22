"""High-level credit manager.

Orchestrates the full credit lifecycle:
  calculate -> reserve -> deduct

Example::

    from ducto import CreditManager, UsageMetrics
    from ducto.interface.supabase import SupabaseStore

    store = SupabaseStore(supabase_client)
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

from typing import Any

from ducto.engine import PricingEngine
from ducto.interface.base import CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    BalanceResult,
    CreditMetadata,
    DeductionResult,
    PricingConfigData,
    ReserveResult,
    SetupResult,
)
from ducto.metrics import UsageMetrics


class InsufficientCreditsError(Exception):
    """Raised when a user does not have enough credits for an operation."""


class PricingNotLoadedError(Exception):
    """Raised when ``deduct()`` is called before pricing is loaded."""


class CreditManager:
    """Orchestrates credit operations: pricing -> reserve -> deduct.

    Args:
        store: A ``CreditStore`` adapter (SupabaseStore, PostgresStore, etc.).
        engine: An optional pre-configured ``PricingEngine``. If omitted,
            call ``load_pricing_from_store()`` or ``load_pricing_from_dict()``
            before ``deduct()``.
    """

    def __init__(
        self,
        store: CreditStore,
        engine: PricingEngine | None = None,
    ) -> None:
        self._store = store
        self._engine = engine

    # -- Schema management -----------------------------------------------

    def setup(self) -> SetupResult:
        """Run bundled SQL migrations through the store."""
        return self._store.setup()

    # -- Pricing configuration -------------------------------------------

    def load_pricing_from_dict(self, data: PricingConfigData | dict[str, Any]) -> None:
        """Load pricing from a ``PricingConfigData`` or raw dict and sync it."""
        raw = data if isinstance(data, dict) else data.model_dump()
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
                "Call load_pricing_from_dict() or set_active_pricing() first."
            )
        engine_dict = active.config.model_dump(exclude_none=True)
        engine_dict["version"] = active.version
        self._engine = PricingEngine.from_dict(engine_dict)

    def publish_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> None:
        """Publish new pricing and update the engine in one call."""
        raw = config.model_dump(exclude_none=True)
        raw["version"] = config.version
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
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
    ) -> AddCreditsResult:
        """Add credits to a user's account."""
        return self._store.add_credits(user_id, amount, type, metadata)

    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str = "usage",
        metadata: CreditMetadata | None = None,
        min_balance: int = 5,
    ) -> ReserveResult:
        """Reserve credits for an upcoming operation."""
        return self._store.reserve_credits(user_id, amount, operation_type, metadata, min_balance)

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
                "PricingEngine not loaded. Call load_pricing_from_dict() "
                "or load_pricing_from_store() first."
            )

        # 1) Calculate cost
        breakdown = self._engine.calculate(metrics)
        cost = int(breakdown.total) if breakdown.total > 0 else 0

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

        return DeductionResult(
            transaction_id=deduction.transaction_id,
            user_id=deduction.user_id,
            amount=deduction.amount,
            balance_after=deduction.balance_after,
            idempotent=deduction.idempotent,
        )

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
