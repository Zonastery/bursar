"""In-memory credit store for testing and development."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from ducto.interface.base import CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    BalanceResult,
    CreditMetadata,
    DeductionResult,
    PricingConfigData,
    PricingConfigResult,
    ReserveResult,
    SetupResult,
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

    # ── Schema management ──────────────────────────────────────────────

    def setup(self) -> SetupResult:
        return SetupResult(
            tables_created=[
                "001_credit_tables.sql",
                "002_credit_rpcs.sql",
                "003_pricing_config.sql",
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

        if available < min_balance:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=0,
                error="insufficient_credits",
            )

        capped = min(amount, available)
        rid = str(uuid.uuid4())
        self._reservations[rid] = _ReservationRecord(
            id=rid,
            user_id=user_id,
            amount=capped,
            operation_type=operation_type,
            metadata=metadata.model_dump() if metadata else {},
        )

        return ReserveResult(
            reservation_id=rid,
            user_id=user_id,
            amount=capped,
            balance=balance,
            reserved_total=reserved_total + capped,
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
        return str(uuid.uuid4())
