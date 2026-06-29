"""Vanilla PostgreSQL-backed credit store adapter.

Connects directly via ``psycopg2``. No Supabase dependency — works with any
Postgres database that has the ducto schema installed.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg2

from ducto.interface.base import CreditStore, StoreError
from ducto.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    BalanceResult,
    CapCheckResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PricingConfigData,
    PricingConfigHistoryItem,
    PricingConfigResult,
    RefundResult,
    ReserveResult,
    SetupResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TopUserRow,
    TransactionRow,
)
from ducto.sql import _get_sql_files


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Coerce a NUMERIC/JSON value to ``Decimal`` (contract §1).

    psycopg2 already returns NUMERIC columns as ``Decimal``; this guards the
    ``None``/``int``/``str`` cases (and a stray ``float``, routed through ``str``
    to avoid binary-float error) so no money value is ever truncated via ``int``.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


class PostgresStore(CreditStore):
    """Credit store backed by a raw Postgres connection.

    Args:
        database_url: Postgres connection string
            (e.g. ``postgresql://user:pass@host:5432/db``).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def _conn(self):
        try:
            return psycopg2.connect(self._database_url)
        except psycopg2.Error as e:
            raise StoreError(f"database connection failed: {e}") from e

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        """Run bundled SQL migrations."""
        result = SetupResult()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                # Bootstrap auth.role() for standalone PG runs (no-op in Supabase)
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_proc p
                            JOIN pg_namespace n ON n.oid = p.pronamespace
                            WHERE n.nspname = 'auth' AND p.proname = 'role'
                        ) THEN
                            CREATE SCHEMA IF NOT EXISTS auth;
                            CREATE FUNCTION auth.role() RETURNS text
                            LANGUAGE SQL IMMUTABLE AS $func$ SELECT 'service_role'::text $func$;
                            CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);
                            CREATE ROLE anon;
                            CREATE ROLE authenticated;
                            CREATE FUNCTION auth.uid() RETURNS uuid
                            LANGUAGE SQL IMMUTABLE AS $func$ SELECT '00000000-0000-0000-0000-000000000000'::uuid $func$;
                        END IF;
                    END
                    $$;
                """)
                conn.commit()

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
            return BalanceResult(user_id=user_id, balance=Decimal(0))

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return BalanceResult(
            user_id=str(result_dict.get("user_id", user_id)),
            balance=_dec(result_dict.get("balance")),
            lifetime_purchased=_dec(result_dict.get("lifetime_purchased")),
        )

    def add_credits(
        self,
        user_id: str,
        amount: Decimal,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        amount = _dec(amount)
        conn = self._conn()
        try:
            meta = metadata.model_dump(mode="json") if metadata else {}
            if expires_at:
                meta["expires_at"] = expires_at.isoformat()
            with conn.cursor() as cur:
                cur.callproc(
                    "credits_add",
                    [user_id, amount, type, json.dumps(meta)],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row else {}
        if "error" in result_dict and result_dict["error"]:
            raise StoreError(f"credits_add failed: {result_dict['error']}")
        return AddCreditsResult(
            transaction_id=str(result_dict.get("id", "")),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=_dec(result_dict.get("amount"), amount),
            new_balance=_dec(result_dict.get("new_balance")),
            lifetime_purchased=_dec(result_dict.get("lifetime_purchased")),
        )

    def reserve_credits(
        self,
        user_id: str,
        amount: Decimal,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: Decimal = Decimal(5),
    ) -> ReserveResult:
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "reserve_credits",
                    [
                        user_id,
                        amount,
                        operation_type,
                        json.dumps(metadata.model_dump(mode="json")) if metadata else "{}",
                        min_balance,
                    ],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if not row:
            return ReserveResult(reservation_id="", user_id=user_id, amount=Decimal(0), error="no result")

        result_dict = row[0] if isinstance(row[0], dict) else {}
        if "error" in result_dict:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=Decimal(0),
                error=str(result_dict["error"]),
            )

        return ReserveResult(
            reservation_id=str(result_dict.get("reservation_id", "")),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=_dec(result_dict.get("amount")),
            balance=_dec(result_dict.get("balance")),
            reserved_total=_dec(result_dict.get("reserved")),
        )

    def deduct_with_allowance(
        self,
        user_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        """Call the atomic ``deduct_with_allowance`` RPC (contract §2).

        The whole calculate-then-charge pipeline runs in one server-side
        transaction; this wrapper only marshals params and maps the JSON envelope
        (success or business-error code) onto ``DeductionResult``.
        """
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}

        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "deduct_with_allowance",
                    [user_id, amount, idempotency_key, min_balance, model, json.dumps(meta)],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        if not result_dict:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=Decimal(0),
                error="no result",
            )
        if "error" in result_dict:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=_dec(result_dict.get("balance_after")),
                error=str(result_dict["error"]),
            )

        return DeductionResult(
            transaction_id=str(result_dict.get("transaction_id", "")),
            user_id=user_id,
            amount=_dec(result_dict.get("amount")),
            allowance_consumed=_dec(result_dict.get("allowance_consumed")),
            balance_after=_dec(result_dict.get("balance_after")),
            idempotent=bool(result_dict.get("idempotent", False)),
            cap_warning=result_dict.get("cap_warning") or None,
        )

    def deduct_credits(
        self,
        user_id: str,
        reservation_id: str,
        amount: Decimal,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json") if metadata else {}
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key

        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "deduct_credits",
                    [user_id, reservation_id, amount, json.dumps(meta)],
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
                balance_after=Decimal(0),
                error="no result",
            )

        result_dict = row[0] if isinstance(row[0], dict) else {}
        if "error" in result_dict:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=Decimal(0),
                error=str(result_dict["error"]),
            )

        return DeductionResult(
            transaction_id=str(result_dict.get("id", "")),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=_dec(result_dict.get("amount"), -amount),
            balance_after=_dec(result_dict.get("new_balance")),
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
                    [json.dumps(config.model_dump(mode="json", exclude_none=True)), label],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return str(result_dict.get("id", ""))

    def get_pricing_history(self) -> list[PricingConfigHistoryItem]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_pricing_configs")
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()

        return [PricingConfigHistoryItem.model_validate(r[0]) for r in rows]

    def get_pricing_config(self, version: int) -> PricingConfigResult | None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_pricing_config", [version])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if row is None:
            return None
        return PricingConfigResult.model_validate(row[0])

    def activate_pricing(self, version: int) -> str:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("activate_pricing_config", [version])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if row is None:
            msg = f"Version {version} not found"
            raise StoreError(msg)
        return str(row[0].get("id", "")) if isinstance(row[0], dict) else str(row[0])

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_user_plan", [user_id])
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return GetUserPlanResult(user_id=user_id, plan_id=None, plan_name=None, free_allowance=Decimal(0))

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return GetUserPlanResult(
            user_id=str(result_dict.get("user_id", user_id)),
            plan_id=result_dict.get("plan_id") or None,
            plan_name=result_dict.get("plan_name") or None,
            free_allowance=_dec(result_dict.get("free_allowance")),
            features=result_dict.get("features") or {},
        )

    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("set_user_plan", [user_id, plan_id])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return SetUserPlanResult(
            user_id=str(result_dict.get("user_id", user_id)),
            plan_id=str(result_dict.get("plan_id", plan_id)),
        )

    def check_allowance(self, user_id: str) -> AllowanceResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("check_plan_allowance", [user_id])
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return AllowanceResult(plan_id="", allowance_remaining=Decimal(0), period_start="", period_end="")

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return AllowanceResult(
            plan_id=str(result_dict.get("plan_id", "")),
            allowance_remaining=_dec(result_dict.get("allowance_remaining")),
            period_start=str(result_dict.get("period_start", "")),
            period_end=str(result_dict.get("period_end", "")),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("increment_usage_window", [user_id, plan_id, _dec(amount)])
            conn.commit()
        finally:
            conn.close()

    # ── Spend caps and rate limiting ────────────────────────────────────

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: Decimal | None = None,
    ) -> CapCheckResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("check_spend_cap", [user_id, model, _dec(amount)])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if not row:
            return CapCheckResult(capped=False, current_spend=Decimal(0), cap_limit=Decimal(0), action=None)

        result_dict = row[0] if isinstance(row[0], dict) else {}
        action = result_dict.get("action")
        return CapCheckResult(
            capped=bool(result_dict.get("capped", False)),
            current_spend=_dec(result_dict.get("current_spend")),
            cap_limit=_dec(result_dict.get("cap_limit")),
            action=action if action in ("deny", "warn", "notify") else None,
            model=str(result_dict.get("model")) if result_dict.get("model") else None,
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "refund_credits",
                    [
                        transaction_id,
                        _dec(amount) if amount is not None else None,
                        reason,
                        json.dumps(metadata.model_dump(mode="json") if metadata else {}),
                    ],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        if "error" in result_dict and result_dict["error"]:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=str(result_dict.get("user_id", "")),
                amount=Decimal(0),
                new_balance=_dec(result_dict.get("new_balance")),
                error=str(result_dict["error"]),
            )

        return RefundResult(
            refund_transaction_id=str(result_dict.get("refund_transaction_id", "")),
            original_transaction_id=transaction_id,
            user_id=str(result_dict.get("user_id", "")),
            amount=_dec(result_dict.get("amount")),
            new_balance=_dec(result_dict.get("new_balance")),
        )

    # ── Usage analytics ─────────────────────────────────────────────────

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("spend_by_user", [start.isoformat(), end.isoformat()])
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        return [
            SpendByUserRow(
                user_id=str(r[0].get("user_id", "")),
                total_spend=_dec(r[0].get("total_spend")),
                transaction_count=int(r[0].get("transaction_count", 0)),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("spend_by_model", [start.isoformat(), end.isoformat()])
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        return [
            SpendByModelRow(
                model=str(r[0].get("model", "")),
                total_spend=_dec(r[0].get("total_spend")),
                transaction_count=int(r[0].get("transaction_count", 0)),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("top_users", [limit, start.isoformat(), end.isoformat()])
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        return [
            TopUserRow(
                user_id=str(r[0].get("user_id", "")),
                total_spend=_dec(r[0].get("total_spend")),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("daily_spend", [start.isoformat(), end.isoformat()])
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        return [
            DailySpendRow(
                date=str(r[0].get("date", "")),
                total_spend=_dec(r[0].get("total_spend")),
                transaction_count=int(r[0].get("transaction_count", 0)),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("aggregate_stats", [start.isoformat(), end.isoformat()])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        if not row or not isinstance(row[0], dict):
            return AggregateStatsRow()
        d = row[0]
        return AggregateStatsRow(
            total_credits_consumed=_dec(d.get("total_credits_consumed")),
            active_users=int(d.get("active_users", 0)),
            avg_daily_spend=_dec(d.get("avg_daily_spend")),
            top_model=str(d.get("top_model", "")),
            top_user=str(d.get("top_user", "")),
        )

    # ── Transaction listing ─────────────────────────────────────────────────

    def list_user_transactions(
        self,
        user_id: str,
        types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TransactionRow]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM list_user_transactions(%s, %s, %s, %s, %s, %s)",
                    [
                        user_id,
                        types,
                        from_date.isoformat() if from_date else None,
                        to_date.isoformat() if to_date else None,
                        limit,
                        offset,
                    ],
                )
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        return [
            TransactionRow(
                id=str(r[0]),
                user_id=str(r[1]),
                amount=_dec(r[2]),
                type=str(r[3]),
                reference_type=str(r[4]) if r[4] else None,
                reference_id=str(r[5]) if r[5] else None,
                metadata=r[6] if isinstance(r[6], dict) else {},
                created_at=str(r[7]),
                total_count=int(r[8]),
            )
            for r in (rows or [])
        ]

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: Decimal = Decimal(0)) -> CreateTeamResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("create_team", [name, _dec(initial_balance)])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return CreateTeamResult(
            team_id=str(result_dict.get("team_id", "")),
            name=str(result_dict.get("name", name)),
        )

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_team_balance", [team_id])
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return TeamBalanceResult(team_id=team_id)

        result_dict = row[0] if isinstance(row[0], dict) else {}
        if "error" in result_dict and result_dict["error"]:
            return TeamBalanceResult(team_id=team_id)

        return TeamBalanceResult(
            team_id=str(result_dict.get("team_id", team_id)),
            name=str(result_dict.get("name", "")),
            balance=_dec(result_dict.get("balance")),
            member_count=int(result_dict.get("member_count", 0)),
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: Decimal | None = None,
    ) -> AddTeamMemberResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "add_team_member",
                    [team_id, user_id, role, _dec(spend_cap) if spend_cap is not None else None],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return AddTeamMemberResult(
            team_id=str(result_dict.get("team_id", team_id)),
            user_id=str(result_dict.get("user_id", user_id)),
            role=str(result_dict.get("role", role)),
        )

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("get_team_members", [team_id])
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()

        return [
            TeamMember(
                user_id=str(r[0].get("user_id", "")),
                role=str(r[0].get("role", "member")),
                spend_cap=_dec(r[0]["spend_cap"]) if r[0].get("spend_cap") is not None else None,
                total_spent=_dec(r[0].get("total_spent")),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: Decimal,
        metadata: CreditMetadata | None = None,
        idempotency_key: str | None = None,
    ) -> TeamDeductionResult:
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        # Thread the idempotency key through metadata (the RPC reads it from
        # metadata->>'idempotency_key'), matching deduct_credits (H12).
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "deduct_team",
                    [team_id, user_id, amount, json.dumps(meta)],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        if "error" in result_dict and result_dict["error"]:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=Decimal(0),
                team_balance_after=_dec(result_dict.get("team_balance_after")),
                error=str(result_dict["error"]),
            )

        return TeamDeductionResult(
            transaction_id=str(result_dict.get("transaction_id", "")),
            team_id=str(result_dict.get("team_id", team_id)),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=_dec(result_dict.get("amount"), -amount),
            team_balance_after=_dec(result_dict.get("team_balance_after")),
        )

    # ── Credit expiry ───────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("expire_credits", [dry_run])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return SweepResult(
            expired_count=int(result_dict.get("expired_count", 0)),
            expired_amount=_dec(result_dict.get("expired_amount")),
            dry_run=dry_run,
        )
