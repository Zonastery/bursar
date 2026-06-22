"""Vanilla PostgreSQL-backed credit store adapter.

Connects directly via ``psycopg2``. No Supabase dependency — works with any
Postgres database that has the ducto schema installed.
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


class PostgresStore(CreditStore):
    """Credit store backed by a raw Postgres connection.

    Args:
        database_url: Postgres connection string
            (e.g. ``postgresql://user:pass@host:5432/db``).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def _conn(self):
        import psycopg2

        return psycopg2.connect(self._database_url)

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        """Run bundled SQL migrations."""
        result = SetupResult()
        conn = self._conn()
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
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_credits_balance", [user_id])
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return BalanceResult(user_id=user_id, balance=0)

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return BalanceResult(
            user_id=str(result_dict.get("user_id", user_id)),
            balance=int(result_dict.get("balance", 0)),
            lifetime_purchased=int(result_dict.get("lifetime_purchased", 0)),
        )

    def add_credits(
        self,
        user_id: str,
        amount: int,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
    ) -> AddCreditsResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "credits_add",
                    [user_id, amount, type, metadata.model_dump(mode="json") if metadata else {}],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return AddCreditsResult(
            transaction_id=str(result_dict.get("id", "")),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=int(result_dict.get("amount", amount)),
            new_balance=int(result_dict.get("new_balance", 0)),
            lifetime_purchased=int(result_dict.get("lifetime_purchased", 0)),
        )

    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: int = 5,
    ) -> ReserveResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "reserve_credits",
                    [
                        user_id,
                        amount,
                        operation_type,
                        metadata.model_dump(mode="json") if metadata else {},
                        min_balance,
                    ],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if not row:
            return ReserveResult(reservation_id="", user_id=user_id, amount=0, error="no result")

        result_dict = row[0] if isinstance(row[0], dict) else {}
        if "error" in result_dict:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=0,
                error=str(result_dict["error"]),
            )

        return ReserveResult(
            reservation_id=str(result_dict.get("reservation_id", "")),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=int(result_dict.get("amount", 0)),
            balance=int(result_dict.get("balance", 0)),
            reserved_total=int(result_dict.get("reserved", 0)),
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

        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "deduct_credits",
                    [user_id, reservation_id, amount, meta],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if not row:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=0,
                error="no result",
            )

        result_dict = row[0] if isinstance(row[0], dict) else {}
        if "error" in result_dict:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=0,
                error=str(result_dict["error"]),
            )

        return DeductionResult(
            transaction_id=str(result_dict.get("id", "")),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=int(result_dict.get("amount", -amount)),
            balance_after=int(result_dict.get("new_balance", 0)),
            idempotent=bool(result_dict.get("idempotent", False)),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_active_pricing_config", [])
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        result_dict = row[0] if isinstance(row[0], dict) else {}
        if not result_dict:
            return None

        return PricingConfigResult.model_validate(result_dict)

    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "set_active_pricing_config",
                    [config.model_dump(mode="json"), label],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return str(result_dict.get("id", ""))
