"""HTTPX-based Supabase credit store adapter.

Makes Supabase RPC calls via ``httpx`` (sync) directly — no supabase-py
dependency in the critical path.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any

import httpx

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

# Business-failure codes a caller may want to inspect on the result model rather
# than have raised as a StoreError (contract §4). Any OTHER `"error"` envelope is
# an unexpected failure and is raised.
_BUSINESS_ERROR_CODES = frozenset(
    {
        "insufficient_credits",
        "cap_reached",
        "unauthorized",
        "not_found",
        "already_refunded",
        "over_refund",
        "invalid_amount",
        "no_balance_record",
        "team_not_found",
        "user_not_in_team",
    }
)


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Coerce a Supabase JSON number to ``Decimal`` via ``str`` (contract §1).

    Supabase returns NUMERIC as JSON numbers (which arrive as Python ``int``/
    ``float``); wrapping through ``str`` avoids binary-float error so no money
    value is truncated or mis-rounded.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return default
    return Decimal(str(value))


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

    # ── Lifecycle ───────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying ``httpx.Client`` (L7)."""
        self._http.close()

    def __enter__(self) -> HttpxSupabaseStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

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

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._key,
            "authorization": f"Bearer {self._key}",
            "content-type": "application/json",
        }

    def _post(self, fn: str, params: dict[str, object]) -> Any:
        """POST to a Supabase RPC and return the parsed JSON body.

        Wraps transport and JSON-decode failures in ``StoreError`` (M10) so no
        raw ``httpx``/``json`` exception leaks out of the store.
        """
        try:
            resp = self._http.post(
                f"{self._url}/rest/v1/rpc/{fn}",
                json=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise StoreError(f"supabase request failed: {e.response.status_code}") from e
        except httpx.TimeoutException as e:
            raise StoreError("supabase request timed out") from e
        except httpx.RequestError as e:
            raise StoreError(f"supabase request error: {e}") from e

        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise StoreError(f"supabase response was not valid JSON: {e}") from e

    def _rpc(self, fn: str, params: dict[str, object]) -> dict[str, Any]:
        """Call a single-object Postgres RPC.

        An unexpected ``{"error": ...}`` envelope (not a known business code) is
        raised as ``StoreError`` (M10); business codes are returned for the caller
        to surface on the result model. PostgREST may also report errors under a
        ``message``/``code`` shape — those are always unexpected, so they raise.
        """
        data = self._post(fn, params)
        if data is None:
            return {}
        if not isinstance(data, dict):
            return {"_value": data}
        err = data.get("error")
        if err is not None and str(err) not in _BUSINESS_ERROR_CODES:
            raise StoreError(f"supabase rpc {fn} returned error: {err}")
        if "message" in data and "code" in data and "error" not in data:
            raise StoreError(f"supabase rpc {fn} failed: {data.get('message')}")
        return data

    def _rpc_list(self, fn: str, params: dict[str, object]) -> list[dict[str, Any]]:
        """Call a Postgres RPC that returns multiple rows."""
        data = self._post(fn, params)
        if data is None:
            return []
        if isinstance(data, dict):
            err = data.get("error")
            if err is not None and str(err) not in _BUSINESS_ERROR_CODES:
                raise StoreError(f"supabase rpc {fn} returned error: {err}")
            if "message" in data and "code" in data and "error" not in data:
                raise StoreError(f"supabase rpc {fn} failed: {data.get('message')}")
            return [data]
        return [r for r in data if r is not None]

    def get_balance(self, user_id: str) -> BalanceResult:
        row = self._rpc("get_credits_balance", {"p_user_id": user_id})
        return BalanceResult(
            user_id=str(row.get("user_id", user_id)),
            balance=_dec(row.get("balance")),
            lifetime_purchased=_dec(row.get("lifetime_purchased")),
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
        meta = metadata.model_dump(mode="json") if metadata else {}
        if expires_at:
            meta["expires_at"] = expires_at.isoformat()
        row = self._rpc(
            "credits_add",
            {
                "p_user_id": user_id,
                # Serialize money as a decimal string so PostgREST casts it to
                # NUMERIC without binary-float error (contract §1).
                "p_amount": str(amount),
                "p_type": type,
                "p_metadata": meta,
            },
        )
        if "error" in row and row["error"]:
            raise StoreError(f"credits_add failed: {row['error']}")
        return AddCreditsResult(
            transaction_id=str(row.get("id", "")),
            user_id=str(row.get("user_id", user_id)),
            amount=_dec(row.get("amount"), amount),
            new_balance=_dec(row.get("new_balance")),
            lifetime_purchased=_dec(row.get("lifetime_purchased")),
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
        row = self._rpc(
            "reserve_credits",
            {
                "p_user_id": user_id,
                "p_amount": str(amount),
                "p_operation_type": operation_type,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
                "p_min_balance": str(min_balance),
            },
        )

        if "error" in row:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=Decimal(0),
                error=str(row["error"]),
            )

        return ReserveResult(
            reservation_id=str(row["reservation_id"]),
            user_id=str(row.get("user_id", user_id)),
            amount=_dec(row.get("amount")),
            balance=_dec(row.get("balance")),
            reserved_total=_dec(row.get("reserved")),
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

        Money params are sent as decimal strings; NUMERIC results come back as
        JSON numbers and are wrapped via ``Decimal(str(...))``. Business-error
        envelopes map onto ``DeductionResult.error``.
        """
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        row = self._rpc(
            "deduct_with_allowance",
            {
                "p_user_id": user_id,
                "p_amount": str(amount),
                "p_idempotency_key": idempotency_key,
                "p_min_balance": str(min_balance),
                "p_model": model,
                "p_metadata": meta,
            },
        )

        if "error" in row:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=_dec(row.get("balance_after")),
                error=str(row["error"]),
            )

        return DeductionResult(
            transaction_id=str(row.get("transaction_id", "")),
            user_id=user_id,
            amount=_dec(row.get("amount")),
            allowance_consumed=_dec(row.get("allowance_consumed")),
            balance_after=_dec(row.get("balance_after")),
            idempotent=bool(row.get("idempotent", False)),
            cap_warning=row.get("cap_warning") or None,
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

        row = self._rpc(
            "deduct_credits",
            {
                "p_user_id": user_id,
                "p_reservation_id": reservation_id,
                "p_amount": str(amount),
                "p_metadata": meta,
            },
        )

        if "error" in row:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=Decimal(0),
                error=str(row["error"]),
            )

        return DeductionResult(
            transaction_id=str(row["id"]),
            user_id=str(row.get("user_id", user_id)),
            amount=_dec(row.get("amount"), -amount),
            balance_after=_dec(row.get("new_balance")),
            idempotent=bool(row.get("idempotent", False)),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        row = self._rpc("get_active_pricing_config", {})
        # No active config: PostgREST returns null/{} (or an error envelope that
        # _rpc would already have raised for non-business codes). Guard against
        # feeding an error/empty payload into model_validate (M10).
        if not row or "error" in row or "config" not in row:
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

    def get_pricing_history(self) -> list[PricingConfigHistoryItem]:
        rows = self._rpc_list("get_pricing_configs", {})
        return [PricingConfigHistoryItem.model_validate(r) for r in rows]

    def get_pricing_config(self, version: int) -> PricingConfigResult | None:
        row = self._rpc("get_pricing_config", {"p_version": version})
        if not row or "error" in row or "config" not in row:
            return None
        return PricingConfigResult.model_validate(row)

    def activate_pricing(self, version: int) -> str:
        row = self._rpc("activate_pricing_config", {"p_version": version})
        err = row.get("error")
        if err:
            raise StoreError(f"Cannot activate pricing: {err}")
        return str(row.get("id", ""))

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        row = self._rpc("get_user_plan", {"p_user_id": user_id})
        if not row:
            return GetUserPlanResult(user_id=user_id, plan_id=None, plan_name=None, free_allowance=Decimal(0))
        return GetUserPlanResult(
            user_id=str(row.get("user_id", user_id)),
            plan_id=row.get("plan_id") or None,
            plan_name=row.get("plan_name") or None,
            free_allowance=_dec(row.get("free_allowance")),
            features=row.get("features") or {},
        )

    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        row = self._rpc("set_user_plan", {"p_user_id": user_id, "p_plan_key": plan_id})
        return SetUserPlanResult(
            user_id=str(row.get("user_id", user_id)),
            plan_id=str(row.get("plan_id", plan_id)),
        )

    def check_allowance(self, user_id: str) -> AllowanceResult:
        row = self._rpc("check_plan_allowance", {"p_user_id": user_id})
        if not row:
            return AllowanceResult(plan_id="", allowance_remaining=Decimal(0), period_start="", period_end="")
        return AllowanceResult(
            plan_id=str(row.get("plan_id", "")),
            allowance_remaining=_dec(row.get("allowance_remaining")),
            period_start=str(row.get("period_start", "")),
            period_end=str(row.get("period_end", "")),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        self._rpc(
            "increment_usage_window",
            {"p_user_id": user_id, "p_plan_id": plan_id, "p_amount": str(_dec(amount))},
        )

    # ── Spend caps and rate limiting ────────────────────────────────────

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: Decimal | None = None,
    ) -> CapCheckResult:
        row = self._rpc(
            "check_spend_cap",
            {"p_user_id": user_id, "p_model": model, "p_amount": str(_dec(amount))},
        )
        if not row or len(row) == 0:
            return CapCheckResult(capped=False, current_spend=Decimal(0), cap_limit=Decimal(0), action=None)
        action = row.get("action")
        return CapCheckResult(
            capped=bool(row.get("capped", False)),
            current_spend=_dec(row.get("current_spend")),
            cap_limit=_dec(row.get("cap_limit")),
            action=action if action in ("deny", "warn", "notify") else None,
            model=str(row["model"]) if row.get("model") else None,
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        row = self._rpc(
            "refund_credits",
            {
                "p_transaction_id": transaction_id,
                "p_amount": str(_dec(amount)) if amount is not None else None,
                "p_reason": reason,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
            },
        )

        if "error" in row and row["error"]:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=str(row.get("user_id", "")),
                amount=Decimal(0),
                new_balance=_dec(row.get("new_balance")),
                error=str(row["error"]),
            )

        return RefundResult(
            refund_transaction_id=str(row.get("refund_transaction_id", "")),
            original_transaction_id=transaction_id,
            user_id=str(row.get("user_id", "")),
            amount=_dec(row.get("amount")),
            new_balance=_dec(row.get("new_balance")),
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
                total_spend=_dec(r.get("total_spend")),
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
                total_spend=_dec(r.get("total_spend")),
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
                total_spend=_dec(r.get("total_spend")),
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
                total_spend=_dec(r.get("total_spend")),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in rows
        ]

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        row = self._rpc("aggregate_stats", {"p_start": start.isoformat(), "p_end": end.isoformat()})
        return AggregateStatsRow(
            total_credits_consumed=_dec(row.get("total_credits_consumed")),
            active_users=int(row.get("active_users", 0)),
            avg_daily_spend=_dec(row.get("avg_daily_spend")),
            top_model=str(row.get("top_model", "")),
            top_user=str(row.get("top_user", "")),
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
        rows = self._rpc_list(
            "list_user_transactions",
            {
                "p_user_id": user_id,
                "p_types": types,
                "p_from_date": from_date.isoformat() if from_date else None,
                "p_to_date": to_date.isoformat() if to_date else None,
                "p_limit": limit,
                "p_offset": offset,
            },
        )
        return [
            TransactionRow(
                id=str(r.get("id", "")),
                user_id=str(r.get("user_id", "")),
                amount=_dec(r.get("amount")),
                type=str(r.get("type", "")),
                reference_type=str(r["reference_type"]) if r.get("reference_type") else None,
                reference_id=str(r["reference_id"]) if r.get("reference_id") else None,
                metadata=r.get("metadata"),
                created_at=str(r.get("created_at", "")),
                total_count=int(r.get("total_count", 0)),
            )
            for r in rows
        ]

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: Decimal = Decimal(0)) -> CreateTeamResult:
        row = self._rpc("create_team", {"p_name": name, "p_initial_balance": str(_dec(initial_balance))})
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
            balance=_dec(row.get("balance")),
            member_count=int(row.get("member_count", 0)),
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: Decimal | None = None,
    ) -> AddTeamMemberResult:
        row = self._rpc(
            "add_team_member",
            {
                "p_team_id": team_id,
                "p_user_id": user_id,
                "p_role": role,
                "p_spend_cap": str(_dec(spend_cap)) if spend_cap is not None else None,
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
                spend_cap=_dec(r["spend_cap"]) if r.get("spend_cap") is not None else None,
                total_spent=_dec(r.get("total_spent")),
            )
            for r in rows
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
        # The RPC reads the key from metadata->>'idempotency_key' (H12).
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key
        row = self._rpc(
            "deduct_team",
            {
                "p_team_id": team_id,
                "p_user_id": user_id,
                "p_amount": str(amount),
                "p_metadata": meta,
            },
        )
        if "error" in row and row["error"]:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=Decimal(0),
                team_balance_after=_dec(row.get("team_balance_after")),
                error=str(row["error"]),
            )
        return TeamDeductionResult(
            transaction_id=str(row.get("transaction_id", "")),
            team_id=str(row.get("team_id", team_id)),
            user_id=str(row.get("user_id", user_id)),
            amount=_dec(row.get("amount"), -amount),
            team_balance_after=_dec(row.get("team_balance_after")),
        )

    # ── Credit expiry ───────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult:
        row = self._rpc("expire_credits", {"p_dry_run": dry_run})
        return SweepResult(
            expired_count=int(row.get("expired_count", 0)),
            expired_amount=_dec(row.get("expired_amount")),
            dry_run=dry_run,
        )
