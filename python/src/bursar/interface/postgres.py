"""Vanilla PostgreSQL-backed credit store adapter.

Connects directly via ``psycopg2``. No Supabase dependency — works with any
Postgres database that has the bursar schema installed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal, cast

import psycopg2
import psycopg2.extras
import psycopg2.pool

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
from bursar.repositories.analytics import AnalyticsRepository
from bursar.repositories.balance import BalanceRepository
from bursar.repositories.bucket import BucketRepository
from bursar.repositories.deduction import DeductionRepository
from bursar.repositories.lease import LeaseRepository
from bursar.repositories.plan import PlanRepository
from bursar.repositories.pricing import PricingRepository
from bursar.repositories.team import TeamRepository
from bursar.sql import _get_sql_files


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Coerce a NUMERIC/JSON value to ``Decimal`` (contract §1).

    psycopg2 already returns NUMERIC columns as ``Decimal``; this guards the
    ``None``/``int``/``str`` cases (and a stray ``float``, routed through ``str``
    to avoid binary-float error) so no money value is ever truncated via ``int``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _feature_period_end(feature_limit: FeatureLimit | None, feature_period_start: date | None) -> date | None:
    """Derive the exclusive feature-limit window end from ``feature_period_start``.

    The store methods (per the ``base.py`` contract) only receive an already
    calendar-aligned ``feature_period_start`` — not an explicit end — so the
    end is derived by re-resolving :func:`resolve_calendar_window` from that
    start date. Because ``feature_period_start`` is, by construction, already
    the aligned start of its own window, feeding it back in as ``now`` yields
    the exact same start plus the correct calendar-aware end (e.g. a
    variable-length month) without needing the caller to pass a redundant end.
    Returns ``None`` when no limit/period is configured (nothing to enforce).
    """
    if feature_limit is None or feature_period_start is None:
        return None
    _, period_end = resolve_calendar_window(datetime.combine(feature_period_start, time.min), feature_limit.period)
    return period_end


def _dec_map(value: Any) -> dict[str, Decimal] | None:
    """Coerce a ``{bucket_key: amount}`` JSONB object into ``dict[str, Decimal]`` (023).

    Used for ``bucket_breakdown``/``expired_by_bucket`` fields, which come back from
    RPCs as a JSON object of tier key -> NUMERIC amount. Returns ``None`` for a
    missing/empty/non-dict value so callers can distinguish "no tier data" from
    "empty breakdown" the same way the rest of this module treats optional fields.
    """
    if not value or not isinstance(value, dict):
        return None
    return {str(k): _dec(v) for k, v in value.items()}


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts ``Decimal`` to a float for JSONB storage."""

    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class PostgresStore(CreditStore):
    """Credit store backed by a raw Postgres connection with pooling.

    Args:
        database_url: Postgres connection string
            (e.g. ``postgresql://user:pass@host:5432/db``).
    """

    def __init__(self, database_url: str, *, pricing_cache_ttl: int = 300) -> None:
        super().__init__(pricing_cache_ttl=pricing_cache_ttl)
        self._database_url = database_url
        self._pool = psycopg2.pool.ThreadedConnectionPool(1, 20, database_url)

    # ── Repository getters ─────────────────────────────────────────────

    @property
    def _balance_repo(self) -> BalanceRepository:
        if not hasattr(self, "__balance_repo"):
            self.__balance_repo = BalanceRepository(self._callproc)
        return self.__balance_repo

    @property
    def _deduction_repo(self) -> DeductionRepository:
        if not hasattr(self, "__deduction_repo"):
            self.__deduction_repo = DeductionRepository(self._callproc)
        return self.__deduction_repo

    @property
    def _lease_repo(self) -> LeaseRepository:
        if not hasattr(self, "__lease_repo"):
            self.__lease_repo = LeaseRepository(self._callproc)
        return self.__lease_repo

    @property
    def _pricing_repo(self) -> PricingRepository:
        if not hasattr(self, "__pricing_repo"):
            self.__pricing_repo = PricingRepository(self._callproc)
        return self.__pricing_repo

    @property
    def _plan_repo(self) -> PlanRepository:
        if not hasattr(self, "__plan_repo"):
            self.__plan_repo = PlanRepository(self._callproc)
        return self.__plan_repo

    @property
    def _analytics_repo(self) -> AnalyticsRepository:
        if not hasattr(self, "__analytics_repo"):
            self.__analytics_repo = AnalyticsRepository(self._callproc)
        return self.__analytics_repo

    @property
    def _team_repo(self) -> TeamRepository:
        if not hasattr(self, "__team_repo"):
            self.__team_repo = TeamRepository(self._callproc)
        return self.__team_repo

    @property
    def _bucket_repo(self) -> BucketRepository:
        if not hasattr(self, "__bucket_repo"):
            self.__bucket_repo = BucketRepository(self._callproc)
        return self.__bucket_repo

    def close(self) -> None:
        """Close all connections in the pool."""
        self._pool.closeall()

    # ── RPC dispatcher ─────────────────────────────────────────────────

    def _callproc(self, name: str, params: list[Any]) -> list[Any]:
        """Execute an RPC and return all result rows, using the connection pool."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.callproc(name, params)
                rows = cur.fetchall()
            conn.commit()
            return [r[0] if isinstance(r, (list, tuple)) else r for r in (rows or [])]
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _conn(self):
        """Create a dedicated connection for one-time operations (e.g. setup)."""
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
        result_dict = self._balance_repo.get_balance(user_id)
        if result_dict is None:
            return BalanceResult(user_id=user_id, balance=Decimal(0))

        return BalanceResult(
            user_id=str(getattr(result_dict, "user_id", user_id)),
            balance=_dec(result_dict.balance),
            lifetime_purchased=_dec(result_dict.lifetime_purchased),
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
        result_dict = self._balance_repo.add_credits(
            user_id,
            str(amount),
            type,
            json.dumps(meta),
            bucket,
            idempotency_key,
        )
        if result_dict.error is not None:
            raise StoreError(f"credits_add failed: {result_dict['error']}")
        return AddCreditsResult(
            transaction_id=str(getattr(result_dict, "id", "")),
            user_id=str(getattr(result_dict, "user_id", user_id)),
            amount=_dec(result_dict.amount, amount),
            new_balance=_dec(result_dict.new_balance),
            lifetime_purchased=_dec(result_dict.lifetime_purchased),
            bucket=str(getattr(result_dict, "bucket", "default")),
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

        The whole calculate-then-charge pipeline runs in one server-side
        transaction; this wrapper only marshals params and maps the JSON envelope
        (success or business-error code) onto ``DeductionResult``.

        ``period_start`` (WS9) is passed as ``p_period_start`` (ISO date string,
        matching the ``expires_at``/date marshalling convention elsewhere in this
        file); ``None`` lets the RPC fall back to the current UTC calendar month.

        ``feature``/``feature_limit``/``feature_period_start`` are threaded into
        the RPC's trailing ``p_feature*`` params; the RPC derives the window end
        itself from ``feature_limit.period`` is NOT possible server-side (the
        cadence lives in Python), so the exclusive window end is computed here
        via :func:`bursar.allowance.resolve_calendar_window` and passed as
        ``p_feature_period_end``. ``feature_limit=None`` skips enforcement
        entirely (the RPC still tags ``metadata.feature`` when ``feature`` is
        given).
        """
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        feature_period_end = _feature_period_end(feature_limit, feature_period_start)

        result_dict = self._deduction_repo.deduct_with_allowance(
            user_id,
            str(amount),
            idempotency_key,
            str(min_balance),
            model,
            json.dumps(meta),
            skip_allowance,
            period_start.isoformat() if period_start is not None else None,
            feature,
            feature_limit.max_calls if feature_limit is not None else None,
            feature_limit.action if feature_limit is not None else None,
            feature_period_start.isoformat() if feature_period_start is not None else None,
            feature_period_end.isoformat() if feature_period_end is not None else None,
        )

        if not result_dict:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=Decimal(0),
                error="no result",
            )
        if result_dict.error is not None:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=_dec(result_dict.balance_after),
                error=str(result_dict.error),
            )

        return DeductionResult(
            transaction_id=str(getattr(result_dict, "transaction_id", "")),
            user_id=user_id,
            amount=_dec(result_dict.amount),
            allowance_consumed=_dec(result_dict.allowance_consumed),
            balance_after=_dec(result_dict.balance_after),
            idempotent=bool(getattr(result_dict, "idempotent", False)),
            cap_warning=result_dict.cap_warning or None,
            feature_limit_warning=result_dict.feature_limit_warning or None,
            bucket_breakdown=_dec_map(result_dict.bucket_breakdown),
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

        params = {
            "user_id": user_id,
            "amount": str(amount),
            "operation_type": operation_type,
            "billing_mode": billing_mode,
            "floor": str(floor),
            "max_concurrent": max_concurrent,
            "ttl_seconds": ttl_seconds,
            "model": model,
            "overdraft_floor": str(overdraft_floor) if overdraft_floor is not None else None,
            "metadata": json.dumps(metadata.model_dump(mode="json")) if metadata else "{}",
            "period_start": period_start.isoformat() if period_start is not None else None,
            "feature": feature,
            "feature_max_calls": feature_limit.max_calls if feature_limit is not None else None,
            "feature_action": feature_limit.action if feature_limit is not None else None,
            "feature_period_start": feature_period_start.isoformat() if feature_period_start is not None else None,
            "feature_period_end": feature_period_end.isoformat() if feature_period_end is not None else None,
        }
        result = self._lease_repo.create_lease(params)

        if not result:
            return LeaseResult(lease_id="", user_id=user_id, error="no result")
        if "error" in result:
            return LeaseResult(
                lease_id="",
                user_id=user_id,
                available=_dec(result.get("available")),
                reserved_total=_dec(result.get("reserved")),
                billing_mode=billing_mode,  # type: ignore[arg-type]
                error=str(result["error"]),
            )
        return LeaseResult(
            lease_id=str(result.get("lease_id", "")),
            user_id=str(result.get("user_id", user_id)),
            amount=_dec(result.get("amount")),
            available=_dec(result.get("available")),
            reserved_total=_dec(result.get("reserved")),
            billing_mode=str(result.get("billing_mode", billing_mode)),  # type: ignore[arg-type]
            expires_at=str(result.get("expires_at", "")),
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

        params = {
            "user_id": user_id,
            "lease_id": lease_id,
            "amount": str(amount),
            "idempotency_key": idempotency_key,
            "min_balance": str(min_balance),
            "model": model,
            "metadata": json.dumps(meta),
            "skip_allowance": skip_allowance,
            "period_start": period_start.isoformat() if period_start is not None else None,
            "feature": feature,
            "feature_max_calls": feature_limit.max_calls if feature_limit is not None else None,
            "feature_action": feature_limit.action if feature_limit is not None else None,
            "feature_period_start": feature_period_start.isoformat() if feature_period_start is not None else None,
            "feature_period_end": feature_period_end.isoformat() if feature_period_end is not None else None,
        }
        result = self._lease_repo.settle_lease(params)

        if not result:
            return DeductionResult(
                transaction_id="", user_id=user_id, amount=Decimal(0), balance_after=Decimal(0), error="no result"
            )
        if "error" in result:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=_dec(result.get("balance_after")),
                error=str(result["error"]),
            )
        return DeductionResult(
            transaction_id=str(result.get("transaction_id", "")),
            user_id=user_id,
            amount=_dec(result.get("amount")),
            allowance_consumed=_dec(result.get("allowance_consumed")),
            balance_after=_dec(result.get("balance_after")),
            idempotent=bool(result.get("idempotent", False)),
            cap_warning=result.get("cap_warning") or None,
            feature_limit_warning=result.get("feature_limit_warning") or None,
            bucket_breakdown=_dec_map(result.get("bucket_breakdown")),
        )

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        result = self._lease_repo.release_lease(user_id, lease_id)
        return ReleaseResult(
            lease_id=lease_id,
            user_id=user_id,
            released=bool(result.get("released", False)),
            reason=result.get("reason"),
        )

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        result = self._lease_repo.renew_lease(user_id, lease_id, ttl_seconds)
        if "error" in result:
            return LeaseResult(lease_id=lease_id, user_id=user_id, error=str(result["error"]))
        return LeaseResult(
            lease_id=str(result.get("lease_id", lease_id)),
            user_id=user_id,
            amount=_dec(result.get("amount")),
            available=_dec(result.get("available")),
            reserved_total=_dec(result.get("reserved")),
            billing_mode=str(result.get("billing_mode", "strict")),  # type: ignore[arg-type]
            expires_at=str(result.get("expires_at", "")),
        )

    def get_available(self, user_id: str) -> AvailableResult:
        result = self._balance_repo.get_available(user_id)
        return AvailableResult(
            user_id=user_id,
            balance=_dec(result.get("balance")),
            reserved=_dec(result.get("reserved")),
            available=_dec(result.get("available")),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        return self._get_cached_pricing(self._load_active_pricing)

    def _load_active_pricing(self) -> PricingConfigResult | None:
        result = self._pricing_repo.get_active_pricing()
        if result is None:
            return None
        return PricingConfigResult.model_validate(result.model_dump())

    def set_active_pricing(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        result_dict = self._pricing_repo.set_active_pricing(json.dumps(config, cls=DecimalEncoder), label)
        self.invalidate_pricing_cache()
        return str(getattr(result_dict, "id", ""))

    def get_pricing_history(self) -> list[PricingConfigHistoryItem]:
        rows = self._pricing_repo.get_pricing_history()
        return [PricingConfigHistoryItem.model_validate(r) for r in rows]

    def get_pricing_config(self, version: int) -> PricingConfigResult | None:
        result = self._pricing_repo.get_pricing_config(version)
        if result is None:
            return None
        return PricingConfigResult.model_validate(result.model_dump())

    def activate_pricing(self, version: int) -> str:
        result_dict = self._pricing_repo.activate_pricing(version)
        if not result_dict:
            msg = f"Version {version} not found"
            raise StoreError(msg)
        self.invalidate_pricing_cache()
        return str(getattr(result_dict, "id", ""))

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        result_dict = self._plan_repo.get_user_plan(user_id)
        if result_dict is None:
            return GetUserPlanResult(user_id=user_id)
        return GetUserPlanResult(
            user_id=str(getattr(result_dict, "user_id", user_id)),
            plan_id=result_dict.plan_id or None,
            plan_label=result_dict.plan_label or None,
            allowance_amount=_dec(result_dict.allowance_amount)
            if result_dict.allowance_amount is not None
            else _dec(0),
            allowance_period=str(result_dict.allowance_period or "calendar_month"),  # type: ignore[arg-type]
            entitlements={k: Entitlement.model_validate(v) for k, v in (result_dict.entitlements or {}).items()},
            billing_mode=str(result_dict.billing_mode or "strict"),  # type: ignore[arg-type]
            per_operation={k: OperationPolicy.model_validate(v) for k, v in (result_dict.per_operation or {}).items()},
            max_concurrent=result_dict.max_concurrent,
            overdraft_floor=_dec(result_dict.overdraft_floor) if result_dict.overdraft_floor is not None else None,
            plan_assigned_at=(
                datetime.fromisoformat(str(result_dict.plan_assigned_at)) if result_dict.plan_assigned_at else None
            ),
            config_version=result_dict.config_version or None,
        )

    def set_user_plan(
        self,
        user_id: str,
        plan_id: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        result_dict = self._plan_repo.set_user_plan(
            user_id,
            plan_id,
            plan_assigned_at.isoformat() if plan_assigned_at else None,
        )
        return SetUserPlanResult(
            user_id=str(getattr(result_dict, "user_id", user_id)),
            plan_id=str(getattr(result_dict, "plan_id", plan_id)),
            plan_assigned_at=str(result_dict.plan_assigned_at) if result_dict.plan_assigned_at else None,
        )

    def unset_user_plan(self, user_id: str) -> dict:
        result_dict = self._plan_repo.unset_user_plan(user_id)
        return {"user_id": str(getattr(result_dict, "user_id", user_id))}

    def migrate_plan_users(
        self,
        plan_key: str,
        target_config_version: int | None = None,
    ) -> MigratePlanUsersResult:
        try:
            result_dict = self._plan_repo.migrate_plan_users(plan_key, target_config_version)
        except psycopg2.Error as e:
            raise StoreError(f"migrate_plan_users failed: {e}") from e

        if not result_dict:
            raise StoreError("migrate_plan_users returned no data")
        if result_dict.error is not None:
            raise StoreError(result_dict.error)
        return MigratePlanUsersResult(
            plan_key=str(getattr(result_dict, "plan_key", plan_key)),
            target_plan_id=str(getattr(result_dict, "target_plan_id", "")),
            target_config_version=int(getattr(result_dict, "target_config_version", 0)),
            migrated_count=int(getattr(result_dict, "migrated_count", 0)),
        )

    def check_allowance(self, user_id: str, period_start: date | None = None) -> AllowanceResult:
        result_dict = self._plan_repo.check_allowance(
            user_id,
            period_start.isoformat() if period_start is not None else None,
        )
        if result_dict is None:
            return AllowanceResult(plan_id="", allowance_remaining=Decimal(0), period_start="", period_end="")
        return AllowanceResult(
            plan_id=str(getattr(result_dict, "plan_id", "")),
            allowance_remaining=_dec(result_dict.allowance_remaining),
            period_start=str(getattr(result_dict, "period_start", "")),
            period_end=str(getattr(result_dict, "period_end", "")),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        self._plan_repo.increment_usage_window(user_id, plan_id, str(_dec(amount)))

    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: date,
        period_end: date,
    ) -> FeatureLimitResult:
        """Call the advisory ``check_feature_limit`` RPC (mirrors ``check_spend_cap``)."""
        result_dict = self._plan_repo.check_feature_limit(
            user_id,
            feature,
            max_calls,
            period_start.isoformat(),
            period_end.isoformat(),
        )
        if result_dict is None:
            return FeatureLimitResult(user_id=user_id, feature=feature, limited=False)
        return FeatureLimitResult(
            user_id=user_id,
            feature=feature,
            limited=bool(getattr(result_dict, "limited", False)),
            limit=int(result_dict.limit or 0),
            used=int(result_dict.used or 0),
            remaining=int(result_dict.remaining or 0),
            period_start=str(getattr(result_dict, "period_start", "")),
            period_end=str(getattr(result_dict, "period_end", "")),
            action=cast(
                "Literal['deny', 'warn', 'notify'] | None",
                str(result_dict.action) if "action" in result_dict else None,
            ),
        )

    # ── Spend caps and rate limiting ────────────────────────────────────

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: Decimal | None = None,
    ) -> CapCheckResult:
        result_dict = self._plan_repo.check_spend_cap(user_id, model, str(_dec(amount)))
        if result_dict is None:
            return CapCheckResult(capped=False, current_spend=Decimal(0), cap_limit=Decimal(0), action=None)
        action = result_dict.action
        return CapCheckResult(
            capped=bool(getattr(result_dict, "capped", False)),
            current_spend=_dec(result_dict.current_spend),
            cap_limit=_dec(result_dict.cap_limit),
            action=action if action in ("deny", "warn", "notify") else None,
            model=str(result_dict.model) if result_dict.model else None,
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        result_dict = self._deduction_repo.refund_credits(
            transaction_id,
            str(_dec(amount)) if amount is not None else None,
            reason,
            json.dumps(metadata.model_dump(mode="json") if metadata else {}),
        )
        if result_dict.error is not None:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=str(getattr(result_dict, "user_id", "")),
                amount=Decimal(0),
                new_balance=_dec(result_dict.new_balance),
                error=str(result_dict.error),
            )
        return RefundResult(
            refund_transaction_id=str(getattr(result_dict, "refund_transaction_id", "")),
            original_transaction_id=transaction_id,
            user_id=str(getattr(result_dict, "user_id", "")),
            amount=_dec(result_dict.amount),
            new_balance=_dec(result_dict.new_balance),
            bucket_breakdown=_dec_map(result_dict.bucket_breakdown),
        )

    def revoke_credits_by_tx_type(self, user_id: str, tx_type: str) -> dict:
        result_dict = self._deduction_repo.revoke_credits_by_tx_type(user_id, tx_type)
        return {
            "user_id": str(getattr(result_dict, "user_id", user_id)),
            "amount": getattr(result_dict, "amount", 0),
            "new_balance": getattr(result_dict, "new_balance", ""),
            "bucket": result_dict.bucket,
        }

    # ── Usage analytics ─────────────────────────────────────────────────

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        rows = self._analytics_repo.spend_by_user(start.isoformat(), end.isoformat())
        return [
            SpendByUserRow(
                user_id=str(r.get("user_id", "")),
                total_spend=_dec(r.get("total_spend")),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in (rows or [])
            if isinstance(r, dict)
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        rows = self._analytics_repo.spend_by_model(start.isoformat(), end.isoformat())
        return [
            SpendByModelRow(
                model=str(r.get("model", "")),
                total_spend=_dec(r.get("total_spend")),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in (rows or [])
            if isinstance(r, dict)
        ]

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        rows = self._analytics_repo.top_users(limit, start.isoformat(), end.isoformat())
        return [
            TopUserRow(
                user_id=str(r.get("user_id", "")),
                total_spend=_dec(r.get("total_spend")),
            )
            for r in (rows or [])
            if isinstance(r, dict)
        ]

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        rows = self._analytics_repo.daily_spend(start.isoformat(), end.isoformat())
        return [
            DailySpendRow(
                date=str(r.get("date", "")),
                total_spend=_dec(r.get("total_spend")),
                transaction_count=int(r.get("transaction_count", 0)),
            )
            for r in (rows or [])
            if isinstance(r, dict)
        ]

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        result = self._analytics_repo.aggregate_stats(start.isoformat(), end.isoformat())
        if not result:
            return AggregateStatsRow()
        return AggregateStatsRow(
            total_credits_consumed=_dec(result.get("total_credits_consumed")),
            active_users=int(result.get("active_users", 0)),
            avg_daily_spend=_dec(result.get("avg_daily_spend")),
            top_model=str(result.get("top_model", "")),
            top_user=str(result.get("top_user", "")),
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
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM public.list_user_transactions(%s, %s, %s, %s, %s, %s)",
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
            self._pool.putconn(conn)
        return [
            TransactionRow(
                id=str(r["id"]),
                user_id=str(r["user_id"]),
                amount=_dec(r["amount"]),
                type=str(r["type"]),
                reference_type=str(r["reference_type"]) if r.get("reference_type") else None,
                reference_id=str(r["reference_id"]) if r.get("reference_id") else None,
                metadata=r.get("metadata"),
                created_at=str(r["created_at"]),
                total_count=int(r["total_count"]),
            )
            for r in (rows or [])
        ]

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: Decimal = Decimal(0)) -> CreateTeamResult:
        result_dict = self._team_repo.create_team(name, str(_dec(initial_balance)))
        return CreateTeamResult(
            team_id=str(getattr(result_dict, "team_id", "")),
            name=str(getattr(result_dict, "name", name)),
        )

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        result_dict = self._team_repo.get_team_balance(team_id)
        if result_dict is None:
            return TeamBalanceResult(team_id=team_id)
        if result_dict.error is not None:
            return TeamBalanceResult(team_id=team_id)
        return TeamBalanceResult(
            team_id=str(getattr(result_dict, "team_id", team_id)),
            name=str(getattr(result_dict, "name", "")),
            balance=_dec(result_dict.balance),
            member_count=int(getattr(result_dict, "member_count", 0)),
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: Decimal | None = None,
    ) -> AddTeamMemberResult:
        result_dict = self._team_repo.add_team_member(
            team_id,
            user_id,
            role,
            str(_dec(spend_cap)) if spend_cap is not None else None,
        )
        return AddTeamMemberResult(
            team_id=str(getattr(result_dict, "team_id", team_id)),
            user_id=str(getattr(result_dict, "user_id", user_id)),
            role=str(getattr(result_dict, "role", role)),
        )

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        rows = self._team_repo.get_team_members(team_id)
        return [
            TeamMember(
                user_id=str(r.get("user_id", "")),
                role=str(r.get("role", "member")),
                spend_cap=_dec(r["spend_cap"]) if r.get("spend_cap") is not None else None,
                total_spent=_dec(r.get("total_spent")),
            )
            for r in (rows or [])
            if isinstance(r, dict)
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
        # metadata->>'idempotency_key') for idempotent replay (H12).
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key
        result_dict = self._team_repo.deduct_team(team_id, user_id, str(amount), json.dumps(meta))
        if result_dict.error is not None:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=Decimal(0),
                team_balance_after=_dec(result_dict.team_balance_after),
                error=str(result_dict.error),
            )
        return TeamDeductionResult(
            transaction_id=str(getattr(result_dict, "transaction_id", "")),
            team_id=str(getattr(result_dict, "team_id", team_id)),
            user_id=str(getattr(result_dict, "user_id", user_id)),
            amount=_dec(result_dict.amount, -amount),
            team_balance_after=_dec(result_dict.team_balance_after),
        )

    # ── Credit expiry ───────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False, user_id: str | None = None) -> SweepResult:
        result_dict = self._bucket_repo.sweep_expired_credits(dry_run, user_id)
        return SweepResult(
            expired_count=int(getattr(result_dict, "expired_count", 0)),
            expired_amount=_dec(result_dict.expired_amount),
            dry_run=dry_run,
            expired_by_bucket=_dec_map(result_dict.expired_by_bucket),
        )

    # ── Credit buckets ────────────────────────────────────────────────

    def get_bucket_balances(self, user_id: str) -> BucketBalancesResult:
        result_dict = self._bucket_repo.get_bucket_balances(user_id)
        buckets = [
            BucketBalance(
                bucket_key=str(t.get("bucket_key", "")),
                label=str(t.get("name", "")),
                priority=int(t.get("priority", 0)),
                expires=bool(t.get("expires", False)),
                balance=_dec(t.get("balance")),
            )
            for t in (result_dict.buckets or [])
        ]
        return BucketBalancesResult(
            user_id=str(getattr(result_dict, "user_id", user_id)),
            buckets=buckets,
            total_balance=_dec(result_dict.total_balance),
        )


def run_migrations(database_url: str) -> SetupResult:
    """Run bundled SQL migrations against *database_url*.

    Standalone entry point for the CLI ``migrate`` command.
    """
    store = PostgresStore(database_url)
    return store.setup(database_url)
