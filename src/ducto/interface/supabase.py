"""Supabase-backed credit store adapter.

Uses ``supabase-py`` client for runtime RPC calls and an optional raw
Postgres connection for schema setup.
"""

from __future__ import annotations

from pathlib import Path

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


def _get_sql_files() -> list[Path]:
    """Return bundled SQL file paths in order."""
    sql_dir = Path(__file__).resolve().parent.parent / "sql"
    return sorted(sql_dir.glob("[0-9]*.sql"))


class SupabaseStore(CreditStore):
    """Credit store backed by Supabase RPCs.

    Args:
        client: Authenticated ``supabase.Client`` instance (service_role key).
        database_url: Postgres connection string for ``psycopg2``, required
            for ``setup()``. If ``None``, setup will raise.
    """

    def __init__(self, client=None, database_url: str | None = None) -> None:
        self._client = client
        self._database_url = database_url

    @classmethod
    def for_setup(cls, database_url: str) -> SupabaseStore:
        """Create a store instance suitable for schema setup only.

        Args:
            database_url: Postgres connection string for ``psycopg2``.

        Returns:
            A ``SupabaseStore`` whose ``setup()`` method runs the bundled SQL
            migrations. Runtime operations (``get_balance``, ``add_credits``,
            etc.) will raise ``NotImplementedError`` — use a full client
            for those.
        """
        return cls(client=None, database_url=database_url)

    # ── Schema management ──────────────────────────────────────────────

    def setup(self) -> SetupResult:
        """Run bundled SQL via raw pg connection."""
        if not self._database_url:
            raise RuntimeError(
                "SupabaseStore.setup() requires database_url. "
                "Pass it to the constructor, or use Supabase CLI / "
                "supabase migration up to run the SQL files manually."
            )

        import psycopg2

        result = SetupResult()
        conn = psycopg2.connect(self._database_url)
        try:
            with conn.cursor() as cur:
                for sql_file in _get_sql_files():
                    sql = sql_file.read_text()
                    try:
                        cur.execute(sql)
                        conn.commit()
                        result.tables_created.append(sql_file.name)
                    except Exception as exc:
                        conn.rollback()
                        result.errors.append(f"{sql_file.name}: {exc}")
        finally:
            conn.close()

        return result

    # ── Runtime operations ─────────────────────────────────────────────

    def get_balance(self, user_id: str) -> BalanceResult:
        data = self._client.rpc("get_credits_balance", {"p_user_id": user_id}).execute()
        row = data.data if data.data else {}
        return BalanceResult(
            user_id=str(row.get("user_id", user_id)),
            balance=int(row.get("balance", 0)),
            lifetime_purchased=int(row.get("lifetime_purchased", 0)),
        )

    def add_credits(
        self,
        user_id: str,
        amount: int,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
    ) -> AddCreditsResult:
        data = self._client.rpc(
            "credits_add",
            {
                "p_user_id": user_id,
                "p_amount": amount,
                "p_type": type,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
            },
        ).execute()
        row = data.data if data.data else {}
        return AddCreditsResult(
            transaction_id=str(row.get("id", "")),
            user_id=str(row.get("user_id", user_id)),
            amount=int(row.get("amount", amount)),
            new_balance=int(row.get("new_balance", 0)),
            lifetime_purchased=int(row.get("lifetime_purchased", 0)),
        )

    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: int = 5,
    ) -> ReserveResult:
        data = self._client.rpc(
            "reserve_credits",
            {
                "p_user_id": user_id,
                "p_amount": amount,
                "p_operation_type": operation_type,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
                "p_min_balance": min_balance,
            },
        ).execute()
        row = data.data if data.data else {}

        if "error" in row:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=0,
                error=str(row["error"]),
            )

        return ReserveResult(
            reservation_id=str(row.get("reservation_id", "")),
            user_id=str(row.get("user_id", user_id)),
            amount=int(row.get("amount", 0)),
            balance=int(row.get("balance", 0)),
            reserved_total=int(row.get("reserved", 0)),
        )

    def deduct_credits(
        self,
        user_id: str,
        reservation_id: str,
        amount: int,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        meta = metadata.model_dump(mode="json") if metadata else {}
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key

        data = self._client.rpc(
            "deduct_credits",
            {
                "p_user_id": user_id,
                "p_reservation_id": reservation_id,
                "p_amount": amount,
                "p_metadata": meta,
            },
        ).execute()
        row = data.data if data.data else {}

        if "error" in row:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=0,
                error=str(row["error"]),
            )

        return DeductionResult(
            transaction_id=str(row.get("id", "")),
            user_id=str(row.get("user_id", user_id)),
            amount=int(row.get("amount", -amount)),
            balance_after=int(row.get("new_balance", 0)),
            idempotent=bool(row.get("idempotent", False)),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        data = self._client.rpc("get_active_pricing_config", {}).execute()
        row = data.data if data.data else None

        if not row:
            return None

        return PricingConfigResult.model_validate(row)

    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        data = self._client.rpc(
            "set_active_pricing_config",
            {"p_config": config.model_dump(mode="json"), "p_label": label},
        ).execute()
        row = data.data if data.data else {}
        return str(row.get("id", ""))
