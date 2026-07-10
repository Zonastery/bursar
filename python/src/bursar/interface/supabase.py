"""HTTPX-based Supabase credit store adapter.

Makes Supabase RPC calls via ``httpx`` (sync) directly — no supabase-py
dependency in the critical path.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from types import TracebackType
from typing import Any

import httpx

from bursar.allowance import resolve_calendar_window
from bursar.interface.base import CreditStore, StoreError
from bursar.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalance,
    BucketBalancesResult,
    CapCheckResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    Entitlement,
    FeatureLimit,
    FeatureLimitResult,
    GetUserPlanResult,
    LeaseResult,
    MigratePlanUsersResult,
    OperationPolicy,
    PricingConfigHistoryItem,
    PricingConfigResult,
    RefundResult,
    ReleaseResult,
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
from bursar.sql import _get_sql_files

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
        # Lease lifecycle business codes (interface plan §3 / M2).
        "concurrency_limit",
        "feature_not_entitled",
        "feature_limit_reached",
        "lease_not_found",
        "lease_expired",
        "lease_released",
        # Credit tier business codes (023).
        "tier_not_found",
        "tier_required",
        "bucket_does_not_expire",
        "expires_at_required",
        "invalid_expires_at",
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


def _dec_map(value: Any) -> dict[str, Decimal] | None:
    """Coerce a ``{bucket_key: amount}`` JSON object into ``dict[str, Decimal]`` (023).

    Used for ``bucket_breakdown``/``expired_by_bucket`` fields. Returns ``None`` for a
    missing/empty/non-dict value so callers can distinguish "no tier data" from
    "empty breakdown" the same way the rest of this module treats optional fields.
    """
    if not value or not isinstance(value, dict):
        return None
    return {str(k): _dec(v) for k, v in value.items()}


def _feature_period_end(feature_limit: FeatureLimit | None, feature_period_start: date | None) -> date | None:
    """Derive the exclusive feature-limit window end from ``feature_period_start``.

    Mirrors ``postgres.py``'s helper of the same name: the store methods (per
    the ``base.py`` contract) only receive an already calendar-aligned
    ``feature_period_start`` — not an explicit end — so the end is derived by
    re-resolving :func:`resolve_calendar_window` from that start date. Returns
    ``None`` when no limit/period is configured (nothing to enforce).
    """
    if feature_limit is None or feature_period_start is None:
        return None
    _, period_end = resolve_calendar_window(datetime.combine(feature_period_start, time.min), feature_limit.period)
    return period_end


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

    def __init__(self, url: str, key: str, *, pricing_cache_ttl: int = 300) -> None:
        super().__init__(pricing_cache_ttl=pricing_cache_ttl)
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
        bucket: str | None = None,
        idempotency_key: str | None = None,
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
                "p_bucket": bucket,
                "p_idempotency_key": idempotency_key,
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
            bucket=str(row.get("bucket", "default")),
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
        skip_allowance: bool = False,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> DeductionResult:
        """Call the atomic ``deduct_with_allowance`` RPC (contract §2).

        Money params are sent as decimal strings; NUMERIC results come back as
        JSON numbers and are wrapped via ``Decimal(str(...))``. Business-error
        envelopes map onto ``DeductionResult.error``.

        ``skip_allowance`` is forwarded as ``p_skip_allowance`` so that
        fixed-cost batch jobs do not consume the user's inference allowance
        (Fix 7 — mirrors the postgres.py and memory.py implementations).

        ``period_start`` (WS9) is forwarded as ``p_period_start`` (an ISO date
        string, matching the ``expires_at`` date marshalling convention
        elsewhere in this file); ``None`` falls back to the current UTC
        calendar month.

        ``feature``/``feature_limit``/``feature_period_start`` are forwarded as
        ``p_feature``/``p_feature_max_calls``/``p_feature_action``/
        ``p_feature_period_start``; the exclusive window end is derived here
        (via :func:`bursar.allowance.resolve_calendar_window`) and forwarded as
        ``p_feature_period_end``. ``feature_limit=None`` skips enforcement
        (the RPC still tags ``metadata.feature`` when ``feature`` is given).
        """
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        feature_period_end = _feature_period_end(feature_limit, feature_period_start)
        row = self._rpc(
            "deduct_with_allowance",
            {
                "p_user_id": user_id,
                "p_amount": str(amount),
                "p_idempotency_key": idempotency_key,
                "p_min_balance": str(min_balance),
                "p_model": model,
                "p_metadata": meta,
                "p_skip_allowance": skip_allowance,
                "p_period_start": period_start.isoformat() if period_start is not None else None,
                "p_feature": feature,
                "p_feature_max_calls": feature_limit.max_calls if feature_limit is not None else None,
                "p_feature_action": feature_limit.action if feature_limit is not None else None,
                "p_feature_period_start": (
                    feature_period_start.isoformat() if feature_period_start is not None else None
                ),
                "p_feature_period_end": feature_period_end.isoformat() if feature_period_end is not None else None,
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
            feature_limit_warning=row.get("feature_limit_warning") or None,
            bucket_breakdown=_dec_map(row.get("bucket_breakdown")),
        )

    # ── Lease lifecycle (atomic admission) ─────────────────────────────

    def create_lease(
        self,
        user_id: str,
        amount: Decimal,
        operation_type: str,
        *,
        billing_mode: str = "strict",
        floor: Decimal = Decimal(0),
        max_concurrent: int | None = None,
        ttl_seconds: int = 600,
        model: str | None = None,
        overdraft_floor: Decimal | None = None,
        metadata: CreditMetadata | None = None,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> LeaseResult:
        amount = _dec(amount)
        floor = _dec(floor)
        feature_period_end = _feature_period_end(feature_limit, feature_period_start)
        row = self._rpc(
            "create_lease",
            {
                "p_user_id": user_id,
                "p_amount": str(amount),
                "p_operation_type": operation_type,
                "p_billing_mode": billing_mode,
                "p_floor": str(floor),
                "p_max_concurrent": max_concurrent,
                "p_ttl_seconds": ttl_seconds,
                "p_model": model,
                "p_overdraft_floor": str(overdraft_floor) if overdraft_floor is not None else None,
                "p_metadata": (metadata.model_dump(mode="json") if metadata else {}),
                "p_period_start": period_start.isoformat() if period_start is not None else None,
                "p_feature": feature,
                "p_feature_max_calls": feature_limit.max_calls if feature_limit is not None else None,
                "p_feature_action": feature_limit.action if feature_limit is not None else None,
                "p_feature_period_start": (
                    feature_period_start.isoformat() if feature_period_start is not None else None
                ),
                "p_feature_period_end": feature_period_end.isoformat() if feature_period_end is not None else None,
            },
        )
        if "error" in row:
            return LeaseResult(
                lease_id="",
                user_id=user_id,
                available=_dec(row.get("available")),
                reserved_total=_dec(row.get("reserved")),
                billing_mode=billing_mode,  # type: ignore[arg-type]
                error=str(row["error"]),
            )
        return LeaseResult(
            lease_id=str(row.get("lease_id", "")),
            user_id=str(row.get("user_id", user_id)),
            amount=_dec(row.get("amount")),
            available=_dec(row.get("available")),
            reserved_total=_dec(row.get("reserved")),
            billing_mode=str(row.get("billing_mode", billing_mode)),  # type: ignore[arg-type]
            expires_at=str(row.get("expires_at", "")),
        )

    def settle_lease(
        self,
        user_id: str,
        lease_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
        skip_allowance: bool = False,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> DeductionResult:
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        feature_period_end = _feature_period_end(feature_limit, feature_period_start)
        row = self._rpc(
            "settle_lease",
            {
                "p_user_id": user_id,
                "p_lease_id": lease_id,
                "p_amount": str(amount),
                "p_idempotency_key": idempotency_key,
                "p_min_balance": str(min_balance),
                "p_model": model,
                "p_metadata": meta,
                "p_skip_allowance": skip_allowance,
                "p_period_start": period_start.isoformat() if period_start is not None else None,
                "p_feature": feature,
                "p_feature_max_calls": feature_limit.max_calls if feature_limit is not None else None,
                "p_feature_action": feature_limit.action if feature_limit is not None else None,
                "p_feature_period_start": (
                    feature_period_start.isoformat() if feature_period_start is not None else None
                ),
                "p_feature_period_end": feature_period_end.isoformat() if feature_period_end is not None else None,
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
            feature_limit_warning=row.get("feature_limit_warning") or None,
            bucket_breakdown=_dec_map(row.get("bucket_breakdown")),
        )

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        row = self._rpc("release_lease", {"p_user_id": user_id, "p_lease_id": lease_id})
        return ReleaseResult(
            lease_id=lease_id,
            user_id=user_id,
            released=bool(row.get("released", False)),
            reason=row.get("reason"),
        )

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        row = self._rpc(
            "renew_lease",
            {"p_user_id": user_id, "p_lease_id": lease_id, "p_ttl_seconds": ttl_seconds},
        )
        if "error" in row:
            return LeaseResult(lease_id=lease_id, user_id=user_id, error=str(row["error"]))
        return LeaseResult(
            lease_id=str(row.get("lease_id", lease_id)),
            user_id=user_id,
            amount=_dec(row.get("amount")),
            available=_dec(row.get("available")),
            reserved_total=_dec(row.get("reserved")),
            billing_mode=str(row.get("billing_mode", "strict")),  # type: ignore[arg-type]
            expires_at=str(row.get("expires_at", "")),
        )

    def get_available(self, user_id: str) -> AvailableResult:
        row = self._rpc("get_available_credits", {"p_user_id": user_id})
        return AvailableResult(
            user_id=user_id,
            balance=_dec(row.get("balance")),
            reserved=_dec(row.get("reserved")),
            available=_dec(row.get("available")),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        return self._get_cached_pricing(self._load_active_pricing)

    def _load_active_pricing(self) -> PricingConfigResult | None:
        row = self._rpc("get_active_pricing_config", {})
        # No active config: PostgREST returns null/{} (or an error envelope that
        # _rpc would already have raised for non-business codes). Guard against
        # feeding an error/empty payload into model_validate (M10).
        if not row or "error" in row or "config" not in row:
            return None
        return PricingConfigResult.model_validate(row)

    def set_active_pricing(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        row = self._rpc(
            "set_active_pricing_config",
            {"p_config": config, "p_label": label},
        )
        self.invalidate_pricing_cache()
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
        self.invalidate_pricing_cache()
        return str(row.get("id", ""))

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        row = self._rpc("get_user_plan", {"p_user_id": user_id})
        if not row:
            return GetUserPlanResult(user_id=user_id)
        return GetUserPlanResult(
            user_id=str(row.get("user_id", user_id)),
            plan_id=row.get("plan_id") or None,
            plan_label=row.get("plan_label") or None,
            allowance_amount=_dec(row["allowance_amount"]) if row.get("allowance_amount") is not None else _dec(0),
            allowance_period=str(row.get("allowance_period") or "calendar_month"),  # type: ignore[arg-type]
            entitlements={k: Entitlement.model_validate(v) for k, v in (row.get("entitlements") or {}).items()},
            billing_mode=str(row.get("billing_mode") or "strict"),  # type: ignore[arg-type]
            per_operation={k: OperationPolicy.model_validate(v) for k, v in (row.get("per_operation") or {}).items()},
            max_concurrent=row.get("max_concurrent"),
            overdraft_floor=_dec(row["overdraft_floor"]) if row.get("overdraft_floor") is not None else None,
            plan_assigned_at=(
                datetime.fromisoformat(str(row["plan_assigned_at"])) if row.get("plan_assigned_at") else None
            ),
            config_version=row.get("config_version") or None,
        )

    def set_user_plan(
        self,
        user_id: str,
        plan_id: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        params: dict = {"p_user_id": user_id, "p_plan_key": plan_id}
        if plan_assigned_at is not None:
            params["p_plan_assigned_at"] = plan_assigned_at.isoformat()
        row = self._rpc("set_user_plan", params)
        return SetUserPlanResult(
            user_id=str(row.get("user_id", user_id)),
            plan_id=str(row.get("plan_id", plan_id)),
            plan_assigned_at=str(row["plan_assigned_at"]) if row.get("plan_assigned_at") else None,
        )

    def unset_user_plan(self, user_id: str) -> dict:
        row = self._rpc("unset_user_plan", {"p_user_id": user_id})
        return {"user_id": str(row.get("user_id", user_id))}

    def migrate_plan_users(
        self,
        plan_key: str,
        target_config_version: int | None = None,
    ) -> MigratePlanUsersResult:
        row = self._rpc(
            "migrate_plan_users",
            {"p_plan_key": plan_key, "p_target_config_version": target_config_version},
        )
        if not row or not isinstance(row, dict):
            raise StoreError("migrate_plan_users returned no data")
        if "error" in row and row["error"]:
            raise StoreError(row["error"])
        return MigratePlanUsersResult(
            plan_key=str(row.get("plan_key", plan_key)),
            target_plan_id=str(row.get("target_plan_id", "")),
            target_config_version=int(row.get("target_config_version", 0)),
            migrated_count=int(row.get("migrated_count", 0)),
        )

    def check_allowance(self, user_id: str, period_start: date | None = None) -> AllowanceResult:
        row = self._rpc(
            "check_plan_allowance",
            {"p_user_id": user_id, "p_period_start": period_start.isoformat() if period_start else None},
        )
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

    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: date,
        period_end: date,
    ) -> FeatureLimitResult:
        """Call the advisory ``check_feature_limit`` RPC (mirrors ``check_spend_cap``)."""
        row = self._rpc(
            "check_feature_limit",
            {
                "p_user_id": user_id,
                "p_feature": feature,
                "p_max_calls": max_calls,
                "p_period_start": period_start.isoformat(),
                "p_period_end": period_end.isoformat(),
            },
        )
        if not row:
            return FeatureLimitResult(user_id=user_id, feature=feature, limited=False)
        return FeatureLimitResult(
            user_id=user_id,
            feature=feature,
            limited=bool(row.get("limited", False)),
            limit=int(row.get("limit") or 0),
            used=int(row.get("used") or 0),
            remaining=int(row.get("remaining") or 0),
            period_start=str(row.get("period_start", "")),
            period_end=str(row.get("period_end", "")),
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
            bucket_breakdown=_dec_map(row.get("bucket_breakdown")),
        )

    def revoke_credits_by_tx_type(self, user_id: str, tx_type: str) -> dict:
        row = self._rpc("revoke_credits_by_tx_type", {"p_user_id": user_id, "p_tx_type": tx_type})
        return {
            "user_id": str(row.get("user_id", user_id)),
            "amount": row.get("amount", 0),
            "new_balance": row.get("new_balance", ""),
            "bucket": row.get("bucket"),
        }

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

    def sweep_expired_credits(self, dry_run: bool = False, user_id: str | None = None) -> SweepResult:
        row = self._rpc("expire_credits", {"p_dry_run": dry_run, "p_user_id": user_id})
        return SweepResult(
            expired_count=int(row.get("expired_count", 0)),
            expired_amount=_dec(row.get("expired_amount")),
            dry_run=dry_run,
            expired_by_bucket=_dec_map(row.get("expired_by_bucket")),
        )

    # ── Credit buckets ────────────────────────────────────────────────

    def get_bucket_balances(self, user_id: str) -> BucketBalancesResult:
        row = self._rpc("get_user_credit_buckets", {"p_user_id": user_id})
        buckets = [
            BucketBalance(
                bucket_key=str(t.get("bucket_key", "")),
                label=str(t.get("name", "")),
                priority=int(t.get("priority", 0)),
                expires=bool(t.get("expires", False)),
                balance=_dec(t.get("balance")),
            )
            for t in (row.get("tiers") or [])
        ]
        return BucketBalancesResult(
            user_id=str(row.get("user_id", user_id)),
            buckets=buckets,
            total_balance=_dec(row.get("total_balance")),
        )
