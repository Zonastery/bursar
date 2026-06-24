"""HTTPX-based Supabase credit store adapter.

Makes Supabase RPC calls via ``httpx`` (sync) directly — no supabase-py
dependency in the critical path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

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


def run_migrations(database_url: str) -> SetupResult:
    """Run bundled SQL migrations against a database.

    Standalone one-shot migration entry point. Idempotent (all DDL uses
    ``IF NOT EXISTS`` / ``CREATE OR REPLACE``).

    Args:
        database_url: Postgres connection string.

    Returns:
        ``SetupResult`` with created tables and any errors.
    """
    import psycopg2

    result = SetupResult()
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            for sql_file in _get_sql_files():
                sql = sql_file.read_text()
                cur.execute(sql)
                conn.commit()
                result.tables_created.append(sql_file.name)

        # Refresh PostgREST schema cache (harmless if no pgrst listener)
        try:
            with conn.cursor() as cur:
                cur.execute("NOTIFY pgrst, 'reload schema'")
                conn.commit()
        except Exception:
            conn.rollback()
    finally:
        conn.close()
    return result


class HttpxSupabaseStore(CreditStore):
    """Credit store backed by Supabase RPCs via raw httpx.

    No dependency on supabase-py's sync client — makes direct HTTP
    POST requests to the Supabase REST API.

    Args:
        url: Supabase project URL (e.g. ``https://<project>.supabase.co``).
        key: Supabase ``service_role`` key.
    """

    def __init__(self, url: str, key: str) -> None:
        self._url = url.rstrip("/")
        self._key = key
        self._http = httpx.Client(timeout=30.0)

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        """Run bundled SQL migrations via ``run_migrations()``.

        Args:
            database_url: Postgres connection string. Required.
        """
        if not database_url:
            raise RuntimeError("HttpxSupabaseStore.setup() requires database_url")
        return run_migrations(database_url)

    # ── Runtime operations ─────────────────────────────────────────────

    def _rpc(self, fn: str, params: dict[str, object]) -> dict[str, Any]:
        """Call a Postgres RPC function via raw HTTP POST."""
        try:
            resp = self._http.post(
                f"{self._url}/rest/v1/rpc/{fn}",
                json=params,
                headers={
                    "apikey": self._key,
                    "authorization": f"Bearer {self._key}",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise StoreError(f"supabase request failed: {e.response.status_code}") from e
        except httpx.TimeoutException as e:
            raise StoreError("supabase request timed out") from e

    def _rpc_list(self, fn: str, params: dict[str, object]) -> list[dict[str, Any]]:
        """Call a Postgres RPC that returns multiple rows."""
        try:
            resp = self._http.post(
                f"{self._url}/rest/v1/rpc/{fn}",
                json=params,
                headers={
                    "apikey": self._key,
                    "authorization": f"Bearer {self._key}",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data is None:
                return []
            if not isinstance(data, list):
                return [data]
            return [r for r in data if r is not None]
        except httpx.HTTPStatusError as e:
            raise StoreError(f"supabase request failed: {e.response.status_code}") from e
        except httpx.TimeoutException as e:
            raise StoreError("supabase request timed out") from e

    def get_balance(self, user_id: str) -> BalanceResult:
        row = self._rpc("get_credits_balance", {"p_user_id": user_id})
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
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        meta = metadata.model_dump(mode="json") if metadata else {}
        if expires_at:
            meta["expires_at"] = expires_at.isoformat()
        row = self._rpc(
            "credits_add",
            {
                "p_user_id": user_id,
                "p_amount": amount,
                "p_type": type,
                "p_metadata": meta,
            },
        )
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
        row = self._rpc(
            "reserve_credits",
            {
                "p_user_id": user_id,
                "p_amount": amount,
                "p_operation_type": operation_type,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
                "p_min_balance": min_balance,
            },
        )

        if "error" in row:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=0,
                error=str(row["error"]),
            )

        return ReserveResult(
            reservation_id=str(row["reservation_id"]),
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

        row = self._rpc(
            "deduct_credits",
            {
                "p_user_id": user_id,
                "p_reservation_id": reservation_id,
                "p_amount": amount,
                "p_metadata": meta,
            },
        )

        if "error" in row:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=0,
                error=str(row["error"]),
            )

        return DeductionResult(
            transaction_id=str(row["id"]),
            user_id=str(row.get("user_id", user_id)),
            amount=int(row.get("amount", -amount)),
            balance_after=int(row["new_balance"]),
            idempotent=bool(row.get("idempotent", False)),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        row = self._rpc("get_active_pricing_config", {})
        if not row:
            return None
        return PricingConfigResult.model_validate(row)

    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        row = self._rpc(
            "set_active_pricing_config",
            {"p_config": config.model_dump(mode="json"), "p_label": label},
        )
        return str(row.get("id", ""))

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        row = self._rpc("get_user_plan", {"p_user_id": user_id})
        if not row:
            return GetUserPlanResult(user_id=user_id, plan_id=None, plan_name=None, free_allowance=0)
        return GetUserPlanResult(
            user_id=str(row.get("user_id", user_id)),
            plan_id=row.get("plan_id") or None,
            plan_name=row.get("plan_name") or None,
            free_allowance=int(row.get("free_allowance", 0)),
        )

    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        row = self._rpc("set_user_plan", {"p_user_id": user_id, "p_plan_id": plan_id})
        return SetUserPlanResult(
            user_id=str(row.get("user_id", user_id)),
            plan_id=str(row.get("plan_id", plan_id)),
        )

    def check_allowance(self, user_id: str) -> AllowanceResult:
        row = self._rpc("check_plan_allowance", {"p_user_id": user_id})
        if not row:
            return AllowanceResult(plan_id="", allowance_remaining=0, period_start="", period_end="")
        return AllowanceResult(
            plan_id=str(row.get("plan_id", "")),
            allowance_remaining=int(row.get("allowance_remaining", 0)),
            period_start=str(row.get("period_start", "")),
            period_end=str(row.get("period_end", "")),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: int) -> None:
        self._rpc(
            "increment_usage_window",
            {"p_user_id": user_id, "p_plan_id": plan_id, "p_amount": amount},
        )

    # ── Spend caps and rate limiting ────────────────────────────────────

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: int | None = None,
    ) -> CapCheckResult:
        row = self._rpc(
            "check_spend_cap",
            {"p_user_id": user_id, "p_model": model, "p_amount": amount or 0},
        )
        if not row or len(row) == 0:
            return CapCheckResult(capped=False, current_spend=0, cap_limit=0, action=None)
        return CapCheckResult(
            capped=bool(row.get("capped", False)),
            current_spend=int(row.get("current_spend", 0)),
            cap_limit=int(row.get("cap_limit", 0)),
            action=str(row["action"]) if row.get("action") else None,
            model=str(row["model"]) if row.get("model") else None,
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: int | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        row = self._rpc(
            "refund_credits",
            {
                "p_transaction_id": transaction_id,
                "p_amount": amount,
                "p_reason": reason,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
            },
        )

        if "error" in row and row["error"]:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=str(row.get("user_id", "")),
                amount=0,
                new_balance=int(row.get("new_balance", 0)),
                error=str(row["error"]),
            )

        return RefundResult(
            refund_transaction_id=str(row.get("refund_transaction_id", "")),
            original_transaction_id=transaction_id,
            user_id=str(row.get("user_id", "")),
            amount=int(row.get("amount", 0)),
            new_balance=int(row.get("new_balance", 0)),
        )

    # ── Usage analytics ─────────────────────────────────────────────────

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        rows = self._rpc_list(
            "spend_by_user",
            {
                "p_start": start.isoformat(),
                "p_end": end.isoformat(),
            },
        )
        return [
            SpendByUserRow(
                user_id=str(r.get("user_id", "")),
                total_spend=int(r.get("total_spend", 0)),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in rows
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        rows = self._rpc_list(
            "spend_by_model",
            {
                "p_start": start.isoformat(),
                "p_end": end.isoformat(),
            },
        )
        return [
            SpendByModelRow(
                model=str(r.get("model", "")),
                total_spend=int(r.get("total_spend", 0)),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in rows
        ]

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        rows = self._rpc_list(
            "top_users",
            {
                "p_limit": limit,
                "p_start": start.isoformat(),
                "p_end": end.isoformat(),
            },
        )
        return [
            TopUserRow(
                user_id=str(r.get("user_id", "")),
                total_spend=int(r.get("total_spend", 0)),
            )
            for r in rows
        ]

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        rows = self._rpc_list(
            "daily_spend",
            {
                "p_start": start.isoformat(),
                "p_end": end.isoformat(),
            },
        )
        return [
            DailySpendRow(
                date=str(r.get("date", "")),
                total_spend=int(r.get("total_spend", 0)),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in rows
        ]

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: int = 0) -> CreateTeamResult:
        row = self._rpc("create_team", {"p_name": name, "p_initial_balance": initial_balance})
        return CreateTeamResult(
            team_id=str(row.get("team_id", "")),
            name=str(row.get("name", name)),
        )

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        row = self._rpc("get_team_balance", {"p_team_id": team_id})
        if not row or "error" in row:
            return TeamBalanceResult(team_id=team_id)
        return TeamBalanceResult(
            team_id=str(row.get("team_id", team_id)),
            name=str(row.get("name", "")),
            balance=int(row.get("balance", 0)),
            member_count=int(row.get("member_count", 0)),
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: int | None = None,
    ) -> AddTeamMemberResult:
        row = self._rpc(
            "add_team_member",
            {
                "p_team_id": team_id,
                "p_user_id": user_id,
                "p_role": role,
                "p_spend_cap": spend_cap,
            },
        )
        return AddTeamMemberResult(
            team_id=str(row.get("team_id", team_id)),
            user_id=str(row.get("user_id", user_id)),
            role=str(row.get("role", role)),
        )

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        rows = self._rpc_list("get_team_members", {"p_team_id": team_id})
        return [
            TeamMember(
                user_id=str(r.get("user_id", "")),
                role=str(r.get("role", "member")),
                spend_cap=r.get("spend_cap"),
                total_spent=int(r.get("total_spent", 0)),
            )
            for r in rows
        ]

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: int,
        metadata: CreditMetadata | None = None,
    ) -> TeamDeductionResult:
        row = self._rpc(
            "deduct_team",
            {
                "p_team_id": team_id,
                "p_user_id": user_id,
                "p_amount": amount,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
            },
        )
        if "error" in row and row["error"]:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=0,
                team_balance_after=int(row.get("team_balance_after", 0)),
                error=str(row["error"]),
            )
        return TeamDeductionResult(
            transaction_id=str(row.get("transaction_id", "")),
            team_id=str(row.get("team_id", team_id)),
            user_id=str(row.get("user_id", user_id)),
            amount=int(row.get("amount", -amount)),
            team_balance_after=int(row.get("team_balance_after", 0)),
        )

    # ── Credit expiry ───────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult:
        row = self._rpc("expire_credits", {"p_dry_run": dry_run})
        return SweepResult(
            expired_count=int(row.get("expired_count", 0)),
            expired_amount=int(row.get("expired_amount", 0)),
            dry_run=dry_run,
        )
