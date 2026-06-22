"""Abstract credit store interface.

All credit operations happen through a ``CreditStore`` adapter. This lets
the package work with Supabase (via RPCs), vanilla PostgreSQL, or in-memory
stores for testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

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
    ) -> AddCreditsResult:
        """Atomically add credits and log a transaction."""
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
