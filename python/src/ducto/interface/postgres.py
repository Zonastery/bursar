"""Vanilla PostgreSQL-backed credit store adapter.

Connects directly via ``psycopg2``. No Supabase dependency — works with any
Postgres database that has the ducto schema installed.
"""

from __future__ import annotations

import json
from datetime import datetime

import psycopg2

from ducto.interface.base import CreditStore, StoreError
from ducto.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AllowanceResult,
    BalanceResult,
    CapCheckResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PricingConfigData,
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
)
from ducto.sql import _get_sql_files


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
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
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
                        json.dumps(metadata.model_dump(mode="json")) if metadata else "{}",
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
                    [json.dumps(config.model_dump(mode="json")), label],
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        result_dict = row[0] if row and isinstance(row[0], dict) else {}
        return str(result_dict.get("id", ""))

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
            return GetUserPlanResult(user_id=user_id, plan_id=None, plan_name=None, free_allowance=0)

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return GetUserPlanResult(
            user_id=str(result_dict.get("user_id", user_id)),
            plan_id=result_dict.get("plan_id") or None,
            plan_name=result_dict.get("plan_name") or None,
            free_allowance=int(result_dict.get("free_allowance", 0)),
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
            return AllowanceResult(plan_id="", allowance_remaining=0, period_start="", period_end="")

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return AllowanceResult(
            plan_id=str(result_dict.get("plan_id", "")),
            allowance_remaining=int(result_dict.get("allowance_remaining", 0)),
            period_start=str(result_dict.get("period_start", "")),
            period_end=str(result_dict.get("period_end", "")),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: int) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("increment_usage_window", [user_id, plan_id, amount])
            conn.commit()
        finally:
            conn.close()

    # ── Spend caps and rate limiting ────────────────────────────────────

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: int | None = None,
    ) -> CapCheckResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("check_spend_cap", [user_id, model, amount or 0])
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        if not row:
            return CapCheckResult(capped=False, current_spend=0, cap_limit=0, action=None)

        result_dict = row[0] if isinstance(row[0], dict) else {}
        return CapCheckResult(
            capped=bool(result_dict.get("capped", False)),
            current_spend=int(result_dict.get("current_spend", 0)),
            cap_limit=int(result_dict.get("cap_limit", 0)),
            action=str(result_dict.get("action")) if result_dict.get("action") else None,
            model=str(result_dict.get("model")) if result_dict.get("model") else None,
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: int | None = None,
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
                        amount,
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
                amount=0,
                new_balance=int(result_dict.get("new_balance", 0)),
                error=str(result_dict["error"]),
            )

        return RefundResult(
            refund_transaction_id=str(result_dict.get("refund_transaction_id", "")),
            original_transaction_id=transaction_id,
            user_id=str(result_dict.get("user_id", "")),
            amount=int(result_dict.get("amount", 0)),
            new_balance=int(result_dict.get("new_balance", 0)),
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
                total_spend=int(r[0].get("total_spend", 0)),
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
                total_spend=int(r[0].get("total_spend", 0)),
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
                total_spend=int(r[0].get("total_spend", 0)),
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
                total_spend=int(r[0].get("total_spend", 0)),
                transaction_count=int(r[0].get("transaction_count", 0)),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: int = 0) -> CreateTeamResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("create_team", [name, initial_balance])
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
            balance=int(result_dict.get("balance", 0)),
            member_count=int(result_dict.get("member_count", 0)),
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: int | None = None,
    ) -> AddTeamMemberResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc("add_team_member", [team_id, user_id, role, spend_cap])
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
                spend_cap=r[0].get("spend_cap"),
                total_spent=int(r[0].get("total_spent", 0)),
            )
            for r in (rows or [])
            if r and isinstance(r[0], dict)
        ]

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: int,
        metadata: CreditMetadata | None = None,
    ) -> TeamDeductionResult:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.callproc(
                    "deduct_team",
                    [team_id, user_id, amount, json.dumps(metadata.model_dump(mode="json") if metadata else {})],
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
                amount=0,
                team_balance_after=int(result_dict.get("team_balance_after", 0)),
                error=str(result_dict["error"]),
            )

        return TeamDeductionResult(
            transaction_id=str(result_dict.get("transaction_id", "")),
            team_id=str(result_dict.get("team_id", team_id)),
            user_id=str(result_dict.get("user_id", user_id)),
            amount=int(result_dict.get("amount", -amount)),
            team_balance_after=int(result_dict.get("team_balance_after", 0)),
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
            expired_amount=int(result_dict.get("expired_amount", 0)),
            dry_run=dry_run,
        )
