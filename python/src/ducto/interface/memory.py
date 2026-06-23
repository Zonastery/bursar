"""In-memory credit store for testing and development."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from ducto.interface.base import CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    AllowanceResult,
    BalanceResult,
    CreditMetadata,
    DeductionResult,
    GetUserPlanResult,
    PlanDefinition,
    PricingConfigData,
    PricingConfigResult,
    RefundResult,
    ReserveResult,
    SetupResult,
    SetUserPlanResult,
)


class _TransactionRecord(BaseModel):
    """Internal transaction record for MemoryStore."""

    id: str
    user_id: str
    amount: int
    type: str
    metadata: dict[str, Any] = {}
    reference_type: str | None = None
    reference_id: str | None = None


class _ReservationRecord(BaseModel):
    """Internal reservation record for MemoryStore."""

    id: str
    user_id: str
    amount: int
    operation_type: str
    metadata: dict[str, Any] = {}


class MemoryStore(CreditStore):
    """Credit store backed by in-memory dicts. Zero dependencies.

    Useful for unit testing and local development without a database.
    All data is lost when the process exits.
    """

    def __init__(self) -> None:
        self._balances: dict[str, int] = {}
        self._lifetime: dict[str, int] = {}
        self._transactions: list[_TransactionRecord] = []
        self._reservations: dict[str, _ReservationRecord] = {}
        self._pricing_config: PricingConfigData | None = None
        self._pricing_version: int = 0
        self._pricing_label: str | None = None
        self._plan_definitions: dict[str, PlanDefinition] = {}
        self._user_plan_map: dict[str, str] = {}
        self._usage_windows: list[dict] = []

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        return SetupResult(
            tables_created=[
                "001_credit_tables.sql",
                "002_credit_rpcs.sql",
                "003_pricing_config.sql",
                "004_user_plans.sql",
                "005_credit_refunds.sql",
            ],
        )

    # ── Runtime operations ─────────────────────────────────────────────

    def get_balance(self, user_id: str) -> BalanceResult:
        return BalanceResult(
            user_id=user_id,
            balance=self._balances.get(user_id, 0),
            lifetime_purchased=self._lifetime.get(user_id, 0),
        )

    def add_credits(
        self,
        user_id: str,
        amount: int,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
    ) -> AddCreditsResult:
        current = self._balances.get(user_id, 0)
        self._balances[user_id] = current + amount
        self._lifetime[user_id] = self._lifetime.get(user_id, 0) + (amount if type == "purchase" else 0)

        tx_id = str(uuid.uuid4())
        self._transactions.append(
            _TransactionRecord(
                id=tx_id,
                user_id=user_id,
                amount=amount,
                type=type,
                metadata=metadata.model_dump() if metadata else {},
            )
        )

        return AddCreditsResult(
            transaction_id=tx_id,
            user_id=user_id,
            amount=amount,
            new_balance=self._balances[user_id],
            lifetime_purchased=self._lifetime[user_id],
        )

    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: int = 5,
    ) -> ReserveResult:
        balance = self._balances.get(user_id, 0)
        reserved_total = sum(r.amount for r in self._reservations.values() if r.user_id == user_id)
        available = balance - reserved_total

        if available - amount < min_balance:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=0,
                error="insufficient_credits",
            )

        rid = str(uuid.uuid4())
        self._reservations[rid] = _ReservationRecord(
            id=rid,
            user_id=user_id,
            amount=amount,
            operation_type=operation_type,
            metadata=metadata.model_dump() if metadata else {},
        )

        return ReserveResult(
            reservation_id=rid,
            user_id=user_id,
            amount=amount,
            balance=balance,
            reserved_total=reserved_total + amount,
        )

    def deduct_credits(
        self,
        user_id: str,
        reservation_id: str,
        amount: int,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        # Idempotency check
        if idempotency_key:
            for tx in self._transactions:
                if tx.metadata.get("idempotency_key") == idempotency_key:
                    return DeductionResult(
                        transaction_id=tx.id,
                        user_id=user_id,
                        amount=-amount,
                        balance_after=self._balances.get(user_id, 0),
                        idempotent=True,
                    )

        current = self._balances.get(user_id, 0)
        if current < amount:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=current,
                error="insufficient_credits",
            )

        self._balances[user_id] = current - amount
        if reservation_id in self._reservations:
            del self._reservations[reservation_id]

        tx_id = str(uuid.uuid4())
        tx_meta = metadata.model_dump() if metadata else {}
        if idempotency_key:
            tx_meta["idempotency_key"] = idempotency_key
        self._transactions.append(
            _TransactionRecord(
                id=tx_id,
                user_id=user_id,
                amount=-amount,
                type="usage",
                metadata=tx_meta,
            )
        )

        return DeductionResult(
            transaction_id=tx_id,
            user_id=user_id,
            amount=-amount,
            balance_after=self._balances[user_id],
            idempotent=False,
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        if self._pricing_config is None:
            return None
        return PricingConfigResult(
            id=str(uuid.uuid4()),
            config=self._pricing_config,
            version=self._pricing_version,
        )

    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        self._pricing_config = config
        self._pricing_version += 1
        self._pricing_label = label
        # Extract plan definitions from v2 config
        plans = getattr(config, "plans", None)
        if plans:
            for plan in plans.values():
                self._plan_definitions[plan.id] = plan
        return str(uuid.uuid4())

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        plan_id = self._user_plan_map.get(user_id)
        plan_def = self._plan_definitions.get(plan_id) if plan_id else None
        return GetUserPlanResult(
            user_id=user_id,
            plan_id=plan_id,
            plan_name=plan_def.name if plan_def else None,
            free_allowance=plan_def.free_allowance if plan_def else 0,
        )

    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        self._user_plan_map[user_id] = plan_id
        return SetUserPlanResult(user_id=user_id, plan_id=plan_id)

    def check_allowance(self, user_id: str) -> AllowanceResult:
        plan_id = self._user_plan_map.get(user_id)
        if not plan_id or plan_id not in self._plan_definitions:
            return AllowanceResult(
                plan_id="",
                allowance_remaining=0,
                period_start="",
                period_end="",
            )
        plan_def = self._plan_definitions[plan_id]
        now = __import__("datetime").datetime.now()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            period_end = now.replace(year=now.year + 1, month=1, day=1) - __import__("datetime").timedelta(days=1)
        else:
            period_end = now.replace(month=now.month + 1, day=1) - __import__("datetime").timedelta(days=1)
        period_end = period_end.replace(hour=0, minute=0, second=0, microsecond=0)
        billing_period = period_start.strftime("%Y-%m-%d")
        usage = sum(
            w["usage"]
            for w in self._usage_windows
            if w["user_id"] == user_id and w["plan_id"] == plan_id and w["billing_period"] == billing_period
        )
        return AllowanceResult(
            plan_id=plan_id,
            allowance_remaining=max(plan_def.free_allowance - usage, 0),
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: int) -> None:
        now = __import__("datetime").datetime.now()
        billing_period = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
        for w in self._usage_windows:
            if w["user_id"] == user_id and w["plan_id"] == plan_id and w["billing_period"] == billing_period:
                w["usage"] += amount
                return
        self._usage_windows.append(
            {
                "user_id": user_id,
                "plan_id": plan_id,
                "billing_period": billing_period,
                "usage": amount,
            }
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: int | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        # Find original transaction
        orig_tx = next((t for t in self._transactions if t.id == transaction_id), None)
        if orig_tx is None:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id="",
                amount=0,
                new_balance=0,
                error="transaction_not_found",
            )

        # Check for duplicate refund
        is_refunded = any(t.type == "refund" and t.reference_id == transaction_id for t in self._transactions)
        if is_refunded:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=orig_tx.user_id,
                amount=0,
                new_balance=self._balances.get(orig_tx.user_id, 0),
                error="already_refunded",
            )

        refund_amount = amount if amount is not None else abs(orig_tx.amount)
        max_refund = abs(orig_tx.amount)
        actual_refund = min(refund_amount, max_refund)

        # Restore balance
        current = self._balances.get(orig_tx.user_id, 0)
        self._balances[orig_tx.user_id] = current + actual_refund

        tx_id = str(uuid.uuid4())
        tx_meta = metadata.model_dump() if metadata else {}
        if reason:
            tx_meta["reason"] = reason
        self._transactions.append(
            _TransactionRecord(
                id=tx_id,
                user_id=orig_tx.user_id,
                amount=actual_refund,
                type="refund",
                reference_type=reason,
                reference_id=transaction_id,
                metadata=tx_meta,
            )
        )

        return RefundResult(
            refund_transaction_id=tx_id,
            original_transaction_id=transaction_id,
            user_id=orig_tx.user_id,
            amount=actual_refund,
            new_balance=self._balances[orig_tx.user_id],
        )
