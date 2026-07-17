"""Vanilla PostgreSQL-backed credit store adapter.

Connects directly via ``psycopg2``. No Supabase dependency — works with any
Postgres database that has the bursar schema installed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
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
    BursarConfigHistoryItem,
    BursarConfigResult,
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
from bursar.repositories.schemas import (
    CreateLeaseParams,
    DeductParams,
    SettleLeaseParams,
)
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
    if isinstance(value, Decimal):
        return value
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, float):
            return Decimal(str(value))
        return Decimal(value)
    except (InvalidOperation, ArithmeticError, ValueError) as e:
        raise StoreError(f"Failed to parse Decimal value: {value!r}") from e


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


BillingMode = Literal["strict", "overdraft"]


def _safe_billing_mode(v: Any, default: BillingMode = "strict") -> BillingMode:
    s = str(v) if v is not None else default
    return cast(BillingMode, s) if s in ("strict", "overdraft") else default


AllowancePeriod = Literal["calendar_month", "rolling_30d", "anniversary"]


def _safe_allowance_period(v: Any, default: AllowancePeriod = "calendar_month") -> AllowancePeriod:
    s = str(v) if v is not None else default
    return cast(AllowancePeriod, s) if s in ("calendar_month", "rolling_30d", "anniversary") else default


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts ``Decimal`` to a string for JSONB storage."""

    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class PostgresStore(CreditStore):
    """Credit store backed by a raw Postgres connection with pooling.

    Args:
        database_url: Postgres connection string
            (e.g. ``postgresql://user:pass@host:5432/db``).
    """

    def __init__(self, database_url: str, *, max_pool_size: int = 20) -> None:
        super().__init__()
        self._database_url = database_url
        self._pool = psycopg2.pool.ThreadedConnectionPool(1, max_pool_size, database_url)

    @property
    def database_url(self) -> str:
        """Postgres connection string for this store (read-only)."""
        return self._database_url

    # ── Repository getters ─────────────────────────────────────────────
    @property
    def _balance_repo(self) -> BalanceRepository:
        if not hasattr(self, "_balance_repo_cache"):
            self._balance_repo_cache = BalanceRepository(self._callproc)
        return self._balance_repo_cache

    @property
    def _deduction_repo(self) -> DeductionRepository:
        if not hasattr(self, "_deduction_repo_cache"):
            self._deduction_repo_cache = DeductionRepository(self._callproc)
        return self._deduction_repo_cache

    @property
    def _lease_repo(self) -> LeaseRepository:
        if not hasattr(self, "_lease_repo_cache"):
            self._lease_repo_cache = LeaseRepository(self._callproc)
        return self._lease_repo_cache

    @property
    def _pricing_repo(self) -> PricingRepository:
        if not hasattr(self, "_pricing_repo_cache"):
            self._pricing_repo_cache = PricingRepository(self._callproc)
        return self._pricing_repo_cache

    @property
    def _plan_repo(self) -> PlanRepository:
        if not hasattr(self, "_plan_repo_cache"):
            self._plan_repo_cache = PlanRepository(self._callproc)
        return self._plan_repo_cache

    @property
    def _analytics_repo(self) -> AnalyticsRepository:
        if not hasattr(self, "_analytics_repo_cache"):
            self._analytics_repo_cache = AnalyticsRepository(self._callproc)
        return self._analytics_repo_cache

    @property
    def _team_repo(self) -> TeamRepository:
        if not hasattr(self, "_team_repo_cache"):
            self._team_repo_cache = TeamRepository(self._callproc)
        return self._team_repo_cache

    @property
    def _bucket_repo(self) -> BucketRepository:
        if not hasattr(self, "_bucket_repo_cache"):
            self._bucket_repo_cache = BucketRepository(self._callproc)
        return self._bucket_repo_cache

    def close(self) -> None:
        """Close all connections in the pool."""
        if self._pool is None:
            return
        self._pool.closeall()
        self._pool = None

    def __del__(self) -> None:
        if hasattr(self, "_pool") and self._pool is not None:
            self.close()

    # ── RPC dispatcher ─────────────────────────────────────────────────

    def _callproc(self, name: str, params: list[Any]) -> list[Any]:
        """Execute an RPC and return all result rows, using the connection pool.

        For single-column results (e.g. JSONB functions), each row is unwrapped
        to its scalar value. For multi-column results (TABLE functions), rows
        are returned as tuples.
        """
        pool = self._pool
        if pool is None:
            raise RuntimeError("cannot call RPC on a closed PostgresStore")
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                # Bursar is deliberately isolated from Supabase's API schema.
                # Keep public as a trailing compatibility namespace for the
                # small number of host-owned auth objects it references.
                cur.execute("SET LOCAL search_path TO bursar, public")
                cur.callproc(name, params)
                rows = cur.fetchall()
            conn.commit()
            return [r[0] if isinstance(r, (list, tuple)) and len(r) == 1 else r for r in (rows or [])]
        except BaseException:
            # Rollback on any exception to avoid pool poisoning
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    def _conn(self):
        """Create a dedicated connection for one-time operations (e.g. setup)."""
        try:
            return psycopg2.connect(self._database_url)
        except psycopg2.Error as e:
            raise StoreError(f"database connection failed: {e}") from e

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        """Apply bundled migrations exactly once, transactionally.

        The old implementation replayed every SQL file and accumulated errors,
        which could leave a partially upgraded database looking successful to
        callers.  A small ledger records the filename and SHA-256 checksum;
        an advisory transaction lock serializes concurrent deploys and any
        failed migration aborts the whole setup transaction.
        """
        result = SetupResult()
        conn = self._conn()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS bursar")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bursar.schema_migrations (
                        version text PRIMARY KEY,
                        checksum text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("bursar:migrations",))
                # Bursar consumes auth.uid()/auth.role() supplied by the host;
                # it never creates Supabase schemas, users, or JWT roles.
                cur.execute("""
                    SELECT to_regnamespace('auth'), to_regprocedure('auth.uid()'), to_regprocedure('auth.role()')
                """)
                auth_row = cur.fetchone()
                if auth_row is None:
                    raise StoreError("auth namespace query returned no rows")
                auth_schema, auth_uid, auth_role = auth_row
                if auth_schema is None or auth_uid is None or auth_role is None:
                    raise StoreError(
                        "Bursar requires configured auth.uid()/auth.role(); refusing to bootstrap auth objects"
                    )

                sql_files = _get_sql_files()

                for sql_file in sql_files:
                    sql = sql_file.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    cur.execute("SELECT checksum FROM bursar.schema_migrations WHERE version = %s", (sql_file.name,))
                    row = cur.fetchone()
                    if row:
                        if row[0] != checksum:
                            raise StoreError(f"migration checksum mismatch for {sql_file.name}")
                        continue
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO bursar.schema_migrations(version, checksum) VALUES (%s, %s)",
                        (sql_file.name, checksum),
                    )
                    result.tables_created.append(sql_file.name)
            conn.commit()
        except StoreError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise StoreError(f"Bursar setup failed transactionally: {exc}") from exc
        finally:
            conn.close()
        return result

    def stamp(self) -> SetupResult:
        """Stamp the current HEAD migrations into the ledger without executing them.

        This is an explicit operator action to recover from a cleared ledger.
        The caller attests that the schema is already at HEAD — the method records
        each migration's checksum without re-applying the SQL.  After stamping,
        subsequent ``setup()`` calls see a complete ledger and are idempotent.

        Uses the same ``read_text().encode("utf-8")`` hashing as ``setup()`` so
        checksums remain consistent regardless of platform newline handling.

        Raises ``StoreError`` if:
        * The bursar schema does not yet exist (run normal migrations instead).
        * The migration ledger already has entries (run normal migrations
          to apply any pending files, or if the ledger is partial recover by
          clearing it first and re-stamping).
        """
        conn = self._conn()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS bursar")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bursar.schema_migrations (
                        version text PRIMARY KEY,
                        checksum text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("bursar:migrations",))

                # Guard: refuse to stamp a database that never had bursar objects
                # (the operator should run normal migrate instead).
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_tables
                        WHERE schemaname = 'bursar' AND tablename = 'credit_buckets'
                    )
                """)
                row = cur.fetchone()
                if row is None or not row[0]:
                    raise StoreError("bursar schema is empty — run 'bursar migrate' to create it, not --baseline-head")

                # Guard: refuse when ledger already has entries — the operator
                # might be trying to stub in a newly-added migration.  They
                # should run normal migrate, or if they are sure, clear the
                # ledger first then re-stamp.
                cur.execute("SELECT count(*) FROM bursar.schema_migrations")
                row = cur.fetchone()
                if row is not None and row[0] > 0:
                    raise StoreError(
                        "migration ledger is not empty — run 'bursar migrate' to "
                        "apply pending migrations, or clear the ledger first "
                        "then retry --baseline-head"
                    )

                sql_files = _get_sql_files()
                result = SetupResult()
                for sql_file in sql_files:
                    sql = sql_file.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    cur.execute(
                        "INSERT INTO bursar.schema_migrations(version, checksum) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (sql_file.name, checksum),
                    )
                    if cur.rowcount > 0:
                        result.tables_created.append(sql_file.name)
            conn.commit()
            return result
        except StoreError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise StoreError(f"Bursar stamp failed: {exc}") from exc
        finally:
            conn.close()

    # ── Runtime operations ─────────────────────────────────────────────

    def get_balance(self, user_id: str) -> BalanceResult:
        """Get the current balance for a user.

        Args:
            user_id: The user ID.

        Returns:
            BalanceResult with user_id, balance, and lifetime_purchased.
            Returns zero balance when the user has no balance record.
        """
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
        """Add credits to a user's balance.

        Args:
            user_id: The user ID.
            amount: The credit amount.
            type: The transaction type (default "adjustment").
            metadata: Optional structured metadata.
            expires_at: Optional expiry datetime for the credits.
            bucket: Target bucket key, or None for default.
            idempotency_key: Idempotency key for replay protection.

        Returns:
            AddCreditsResult with transaction details.

        Raises:
            StoreError: If the RPC returns no result or an error.
        """
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json") if metadata else {}
        if expires_at:
            meta["expires_at"] = expires_at.isoformat()
        result = self._balance_repo.add_credits(
            user_id,
            str(amount),
            type,
            json.dumps(meta),
            bucket,
            idempotency_key,
        )
        if result is None:
            raise StoreError("credits_add returned no result")
        if result.error is not None:
            raise StoreError(f"credits_add failed: {result.error}")
        return AddCreditsResult(
            transaction_id=str(getattr(result, "id", "")),
            user_id=str(getattr(result, "user_id", user_id)),
            amount=_dec(result.amount, amount),
            new_balance=_dec(result.new_balance),
            lifetime_purchased=_dec(result.lifetime_purchased),
            bucket=str(getattr(result, "bucket", "default")),
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

        params = DeductParams(
            user_id=user_id,
            amount=str(amount),
            idempotency_key=idempotency_key,
            min_balance=str(min_balance),
            model=model,
            metadata=json.dumps(meta),
            skip_allowance=skip_allowance,
            period_start=period_start.isoformat() if period_start is not None else None,
            feature=feature,
            feature_max_calls=feature_limit.max_calls if feature_limit is not None else None,
            feature_action=feature_limit.action if feature_limit is not None else None,
            feature_period_start=feature_period_start.isoformat() if feature_period_start is not None else None,
            feature_period_end=feature_period_end.isoformat() if feature_period_end is not None else None,
        )
        result = self._deduction_repo.deduct_with_allowance(params)

        if result is None:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=Decimal(0),
                error="no result",
            )
        if result.error is not None:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=_dec(result.balance_after),
                error=str(result.error),
            )

        return DeductionResult(
            transaction_id=str(getattr(result, "transaction_id", "")),
            user_id=user_id,
            amount=_dec(result.amount),
            allowance_consumed=_dec(result.allowance_consumed),
            balance_after=_dec(result.balance_after),
            idempotent=bool(getattr(result, "idempotent", False)),
            cap_warning=result.cap_warning or None,
            feature_limit_warning=result.feature_limit_warning or None,
            bucket_breakdown=_dec_map(result.bucket_breakdown),
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
        """Create a credit lease (reservation) for admission control.

        Args:
            user_id: The user ID.
            amount: The worst-case amount to reserve.
            operation_type: The operation type key.
            billing_mode: Billing mode policy ("strict", "overdraft").
            floor: Minimum balance floor during lease.
            max_concurrent: Max concurrent leases for this user, or None.
            ttl_seconds: Time-to-live for the lease in seconds.
            model: The AI model identifier, or None.
            overdraft_floor: Overdraft floor, or None.
            metadata: Optional structured metadata.
            period_start: Calendar period start date, or None.
            feature: Feature key for feature limit enforcement, or None.
            feature_limit: Feature limit configuration, or None.
            feature_period_start: Feature limit period start, or None.

        Returns:
            LeaseResult with lease_id and reservation details.

        Raises:
            StoreError: If the RPC returns no result (admission denied).
        """
        amount = _dec(amount)
        floor = _dec(floor)
        feature_period_end = _feature_period_end(feature_limit, feature_period_start)

        params = CreateLeaseParams(
            user_id=user_id,
            amount=str(amount),
            operation_type=operation_type,
            billing_mode=billing_mode,
            floor=str(floor),
            max_concurrent=str(max_concurrent) if max_concurrent is not None else None,
            ttl_seconds=ttl_seconds,
            model=model,
            overdraft_floor=str(overdraft_floor) if overdraft_floor is not None else None,
            metadata=json.dumps(metadata.model_dump(mode="json")) if metadata else "{}",
            period_start=period_start.isoformat() if period_start is not None else None,
            feature=feature,
            feature_max_calls=feature_limit.max_calls if feature_limit is not None else None,
            feature_action=feature_limit.action if feature_limit is not None else None,
            feature_period_start=feature_period_start.isoformat() if feature_period_start is not None else None,
            feature_period_end=feature_period_end.isoformat() if feature_period_end is not None else None,
        )
        result = self._lease_repo.create_lease(params)

        if result is None:
            return LeaseResult(lease_id="", user_id=user_id, error="no result")
        if result.error is not None:
            return LeaseResult(
                lease_id="",
                user_id=user_id,
                available=_dec(result.available),
                reserved_total=_dec(result.reserved),
                billing_mode=_safe_billing_mode(billing_mode),
                error=str(result.error),
            )
        return LeaseResult(
            lease_id=str(getattr(result, "lease_id", "")),
            user_id=user_id,
            amount=_dec(result.amount),
            available=_dec(result.available),
            reserved_total=_dec(result.reserved),
            billing_mode=_safe_billing_mode(str(getattr(result, "billing_mode", billing_mode))),
            expires_at=getattr(result, "expires_at", None),
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
        """Settle a lease by deducting the actual amount used.

        Args:
            user_id: The user ID.
            lease_id: The lease ID to settle.
            amount: The actual amount to charge.
            idempotency_key: Idempotency key for replay protection.
            min_balance: Minimum balance floor after deduction.
            model: The AI model identifier, or None.
            metadata: Optional structured metadata.
            skip_allowance: If True, skip plan allowance checks.
            period_start: Calendar period start date, or None.
            feature: Feature key for feature limit enforcement, or None.
            feature_limit: Feature limit configuration, or None.
            feature_period_start: Feature limit period start, or None.

        Returns:
            DeductionResult with transaction details.
        """
        amount = _dec(amount)
        min_balance = _dec(min_balance)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        feature_period_end = _feature_period_end(feature_limit, feature_period_start)

        params = SettleLeaseParams(
            user_id=user_id,
            lease_id=lease_id,
            amount=str(amount),
            idempotency_key=idempotency_key,
            min_balance=str(min_balance),
            model=model,
            metadata=json.dumps(meta),
            skip_allowance=skip_allowance,
            period_start=period_start.isoformat() if period_start is not None else None,
            feature=feature,
            feature_max_calls=feature_limit.max_calls if feature_limit is not None else None,
            feature_action=feature_limit.action if feature_limit is not None else None,
            feature_period_start=feature_period_start.isoformat() if feature_period_start is not None else None,
            feature_period_end=feature_period_end.isoformat() if feature_period_end is not None else None,
        )
        result = self._lease_repo.settle_lease(params)

        if result is None:
            return DeductionResult(
                transaction_id="", user_id=user_id, amount=Decimal(0), balance_after=Decimal(0), error="no result"
            )
        if result.error is not None:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=_dec(result.balance_after),
                error=str(result.error),
            )
        return DeductionResult(
            transaction_id=str(getattr(result, "transaction_id", "")),
            user_id=user_id,
            amount=_dec(result.amount),
            allowance_consumed=_dec(result.allowance_consumed),
            balance_after=_dec(result.balance_after),
            idempotent=bool(getattr(result, "idempotent", False)),
            cap_warning=result.cap_warning or None,
            feature_limit_warning=result.feature_limit_warning or None,
            bucket_breakdown=_dec_map(result.bucket_breakdown),
        )

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        """Release a lease without deducting credits (cancels the reservation).

        Args:
            user_id: The user ID.
            lease_id: The lease ID to release.

        Returns:
            ReleaseResult indicating whether the release was successful.
        """
        result = self._lease_repo.release_lease(user_id, lease_id)
        if result is None:
            return ReleaseResult(lease_id=lease_id, user_id=user_id, released=False)
        return ReleaseResult(
            lease_id=lease_id,
            user_id=user_id,
            released=bool(getattr(result, "released", False)),
            reason=result.reason,
        )

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        """Extend the TTL of an existing lease.

        Args:
            user_id: The user ID.
            lease_id: The lease ID to renew.
            ttl_seconds: The new TTL in seconds from now.

        Returns:
            LeaseResult with updated expiry information.
        """
        result = self._lease_repo.renew_lease(user_id, lease_id, ttl_seconds)
        if result is None:
            return LeaseResult(lease_id=lease_id, user_id=user_id, error="no result")
        if result.error is not None:
            return LeaseResult(lease_id=lease_id, user_id=user_id, error=str(result.error))
        return LeaseResult(
            lease_id=str(getattr(result, "lease_id", lease_id)),
            user_id=user_id,
            amount=_dec(result.amount),
            available=_dec(result.available),
            reserved_total=_dec(result.reserved),
            billing_mode=_safe_billing_mode(str(getattr(result, "billing_mode", "strict"))),
            expires_at=getattr(result, "expires_at", None),
        )

    def get_available(self, user_id: str) -> AvailableResult:
        """Get the available (unreserved) credit balance for a user.

        Args:
            user_id: The user ID.

        Returns:
            AvailableResult with balance, reserved, and available amounts.
        """
        result = self._balance_repo.get_available(user_id)
        if result is None:
            return AvailableResult(user_id=user_id, balance=Decimal(0), reserved=Decimal(0), available=Decimal(0))
        return AvailableResult(
            user_id=user_id,
            balance=_dec(result.balance),
            reserved=_dec(result.reserved),
            available=_dec(result.available),
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> BursarConfigResult | None:
        return self._load_active_pricing()

    def _normalize_bursar_config(self, result: Any) -> BursarConfigResult | None:
        """Normalize a raw pricing config DB result into BursarConfigResult."""
        if result is None:
            return None
        return BursarConfigResult.model_validate(result.model_dump())

    def _load_active_pricing(self) -> BursarConfigResult | None:
        return self._normalize_bursar_config(self._pricing_repo.get_active_pricing())

    def set_active_pricing(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        """Set a new active pricing configuration.

        Args:
            config: The pricing configuration dict.
            label: Optional human-readable label.

        Returns:
            The ID of the newly activated pricing config.

        Raises:
            StoreError: If the RPC returns no result.
            ConfigError: If the config fails validation.
        """
        from bursar.config import canonical_bursar_config_dict

        canonical = canonical_bursar_config_dict(config)
        result = self._pricing_repo.set_active_pricing(json.dumps(canonical, cls=DecimalEncoder), label)
        if result is None:
            raise StoreError("set_active_pricing returned no result")
        return str(getattr(result, "id", ""))

    def get_pricing_history(self) -> list[BursarConfigHistoryItem]:
        """Get all pricing configuration versions.

        Returns:
            List of BursarConfigHistoryItem (may be empty).
        """
        rows = self._pricing_repo.get_pricing_history()
        return [
            BursarConfigHistoryItem(
                id=str(r.id),
                version=r.version,
                label=r.label,
                active=r.active,
                created_at=str(r.created_at),
            )
            for r in rows
        ]

    def get_bursar_config(self, version: int) -> BursarConfigResult | None:
        """Get a specific pricing configuration by version number.

        Args:
            version: The version number to retrieve.

        Returns:
            BursarConfigResult if found, None otherwise.
        """
        return self._normalize_bursar_config(self._pricing_repo.get_bursar_config(version))

    def activate_pricing(self, version: int) -> str:
        """Activate a specific pricing configuration version.

        Args:
            version: The version number to activate.

        Returns:
            The ID of the activated config.

        Raises:
            StoreError: If the version is not found.
        """
        result = self._pricing_repo.activate_pricing(version)
        if result is None:
            msg = f"Version {version} not found"
            raise StoreError(msg)
        return str(getattr(result, "id", ""))

    def publish_pricing(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        """Publish an inactive pricing configuration draft."""
        from bursar.config import canonical_bursar_config_dict

        canonical = canonical_bursar_config_dict(config)
        result = self._pricing_repo.publish_pricing(json.dumps(canonical, cls=DecimalEncoder), label)
        if result is None:
            raise StoreError("publish_pricing returned no result")
        return str(getattr(result, "id", ""))

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        """Get the current plan for a user.

        Args:
            user_id: The user ID.

        Returns:
            GetUserPlanResult with plan details or defaults if no plan assigned.
        """
        result = self._plan_repo.get_user_plan(user_id)
        if result is None:
            return GetUserPlanResult(user_id=user_id)
        return GetUserPlanResult(
            user_id=str(getattr(result, "user_id", user_id)),
            plan_id=result.plan_id or None,
            plan_label=result.plan_label or None,
            allowance_amount=_dec(result.allowance_amount) if result.allowance_amount is not None else _dec(0),
            allowance_period=_safe_allowance_period(str(result.allowance_period or "calendar_month")),
            entitlements={k: Entitlement.model_validate(v) for k, v in (result.entitlements or {}).items()},
            rate_overrides={str(k): str(v) for k, v in (result.rate_overrides or {}).items()},
            billing_mode=_safe_billing_mode(str(result.billing_mode or "strict")),
            per_operation={k: OperationPolicy.model_validate(v) for k, v in (result.per_operation or {}).items()},
            max_concurrent=result.max_concurrent,
            overdraft_floor=_dec(result.overdraft_floor) if result.overdraft_floor is not None else None,
            plan_assigned_at=(
                datetime.fromisoformat(str(result.plan_assigned_at)) if result.plan_assigned_at else None
            ),
            config_version=result.config_version or None,
            catalog_version=result.catalog_version or result.config_version or None,
        )

    def set_user_plan(
        self,
        user_id: str,
        plan_id: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        """Assign a plan to a user.

        Args:
            user_id: The user ID.
            plan_id: The plan identifier.
            plan_assigned_at: The assignment datetime, or None for now.

        Returns:
            SetUserPlanResult with assignment details.

        Raises:
            StoreError: If the RPC returns no result.
        """
        result = self._plan_repo.set_user_plan(
            user_id,
            plan_id,
            plan_assigned_at.isoformat() if plan_assigned_at else None,
        )
        if result is None:
            raise StoreError("set_user_plan returned no result")
        return SetUserPlanResult(
            user_id=str(getattr(result, "user_id", user_id)),
            plan_id=str(getattr(result, "plan_id", plan_id)),
            plan_assigned_at=getattr(result, "plan_assigned_at", None),
        )

    def unset_user_plan(self, user_id: str) -> dict:
        """Remove the plan assignment from a user.

        Args:
            user_id: The user ID.

        Returns:
            Dict with the user_id.
        """
        result = self._plan_repo.unset_user_plan(user_id)
        if result is None:
            return {"user_id": user_id}
        return {"user_id": str(getattr(result, "user_id", user_id))}

    def migrate_plan_users(
        self,
        plan_key: str,
        target_config_version: int | None = None,
    ) -> MigratePlanUsersResult:
        """Migrate all users on a plan key to a new config version.

        Args:
            plan_key: The plan key to migrate users from.
            target_config_version: The target config version, or None.

        Returns:
            MigratePlanUsersResult with migration counts.

        Raises:
            StoreError: If the RPC fails or returns no data.
        """
        try:
            result = self._plan_repo.migrate_plan_users(plan_key, target_config_version)
        except psycopg2.Error as e:
            raise StoreError(f"migrate_plan_users failed: {e}") from e

        if result is None:
            raise StoreError("migrate_plan_users returned no data")
        if result.error is not None:
            raise StoreError(result.error)
        return MigratePlanUsersResult(
            plan_key=str(getattr(result, "plan_key", plan_key)),
            target_plan_id=str(getattr(result, "target_plan_id", "")),
            target_config_version=int(getattr(result, "target_config_version", 0)),
            migrated_count=int(getattr(result, "migrated_count", 0)),
        )

    def check_allowance(self, user_id: str, period_start: date | None = None) -> AllowanceResult:
        """Check the remaining plan allowance for a user.

        Args:
            user_id: The user ID.
            period_start: The period start date, or None for current period.

        Returns:
            AllowanceResult with remaining allowance or zero defaults.
        """
        result = self._plan_repo.check_allowance(
            user_id,
            period_start.isoformat() if period_start is not None else None,
        )
        if result is None:
            return AllowanceResult(plan_id="", allowance_remaining=Decimal(0), period_start=None, period_end=None)
        return AllowanceResult(
            plan_id=str(getattr(result, "plan_id", "")),
            allowance_remaining=_dec(result.allowance_remaining),
            period_start=getattr(result, "period_start", None),
            period_end=getattr(result, "period_end", None),
        )

    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: date,
        period_end: date,
    ) -> FeatureLimitResult:
        """Call the advisory ``check_feature_limit`` RPC."""
        result = self._plan_repo.check_feature_limit(
            user_id,
            feature,
            max_calls,
            period_start.isoformat(),
            period_end.isoformat(),
        )
        if result is None:
            return FeatureLimitResult(user_id=user_id, feature=feature, limited=False)
        return FeatureLimitResult(
            user_id=user_id,
            feature=feature,
            limited=bool(getattr(result, "limited", False)),
            limit=int(result.limit or 0),
            used=int(result.used or 0),
            remaining=int(result.remaining or 0),
            period_start=getattr(result, "period_start", None),
            period_end=getattr(result, "period_end", None),
            action=(
                result.action
                if isinstance(result.action, str) and result.action in ("deny", "warn", "notify")
                else None
            ),
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        """Refund a previous credit transaction.

        Args:
            transaction_id: The original transaction ID to refund.
            amount: The amount to refund, or None for full refund.
            reason: The refund reason, or None.
            metadata: Optional structured metadata.

        Returns:
            RefundResult with refund transaction details.
        """
        result = self._deduction_repo.refund_credits(
            transaction_id,
            str(_dec(amount)) if amount is not None else None,
            reason,
            json.dumps(metadata.model_dump(mode="json") if metadata else {}),
        )
        if result is None:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id="",
                amount=Decimal(0),
                new_balance=Decimal(0),
                error="no result",
            )
        if result.error is not None:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=str(getattr(result, "user_id", "")),
                amount=Decimal(0),
                new_balance=_dec(result.new_balance),
                error=str(result.error),
            )
        return RefundResult(
            refund_transaction_id=str(getattr(result, "refund_transaction_id", "")),
            original_transaction_id=transaction_id,
            user_id=str(getattr(result, "user_id", "")),
            amount=_dec(result.amount),
            new_balance=_dec(result.new_balance),
            bucket_breakdown=_dec_map(result.bucket_breakdown),
        )

    def revoke_credits_by_tx_type(self, user_id: str, tx_type: str) -> dict:
        """Revoke credits for all transactions of a given type for a user.

        Args:
            user_id: The user ID.
            tx_type: The transaction type to revoke.

        Returns:
            Dict with user_id, amount, new_balance, and bucket.
        """
        result = self._deduction_repo.revoke_credits_by_tx_type(user_id, tx_type)
        if result is None:
            return {"user_id": user_id, "amount": 0, "new_balance": "", "bucket": None}
        return {
            "user_id": str(getattr(result, "user_id", user_id)),
            "amount": str(_dec(getattr(result, "amount", 0))),
            "new_balance": str(_dec(getattr(result, "new_balance", 0))),
            "bucket": getattr(result, "bucket", None) if hasattr(result, "bucket") else None,
        }

    # ── Usage analytics ─────────────────────────────────────────────────

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Get total spend grouped by user within a date range.

        Args:
            start: The range start datetime.
            end: The range end datetime.

        Returns:
            List of SpendByUserRow (may be empty).
        """
        rows = self._analytics_repo.spend_by_user(start.isoformat(), end.isoformat())
        return [
            SpendByUserRow(
                user_id=str(r.user_id),
                total_spend=_dec(r.total_spend),
                transaction_count=int(r.transaction_count),
            )
            for r in rows
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Get total spend grouped by model within a date range.

        Args:
            start: The range start datetime.
            end: The range end datetime.

        Returns:
            List of SpendByModelRow (may be empty).
        """
        rows = self._analytics_repo.spend_by_model(start.isoformat(), end.isoformat())
        return [
            SpendByModelRow(
                model=str(r.model),
                total_spend=_dec(r.total_spend),
                transaction_count=int(r.transaction_count),
            )
            for r in rows
        ]

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        """Get the top users by spend within a date range.

        Args:
            limit: Maximum number of users to return.
            start: The range start datetime.
            end: The range end datetime.

        Returns:
            List of TopUserRow (may be empty).
        """
        rows = self._analytics_repo.top_users(limit, start.isoformat(), end.isoformat())
        return [
            TopUserRow(
                user_id=str(r.user_id),
                total_spend=_dec(r.total_spend),
            )
            for r in rows
        ]

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        """Get total spend broken down by day within a date range.

        Args:
            start: The range start datetime.
            end: The range end datetime.

        Returns:
            List of DailySpendRow (may be empty).
        """
        rows = self._analytics_repo.daily_spend(start.isoformat(), end.isoformat())
        return [
            DailySpendRow(
                date=str(r.date),
                total_spend=_dec(r.total_spend),
                transaction_count=int(r.transaction_count),
            )
            for r in rows
        ]

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        """Get aggregate usage statistics for a date range.

        Args:
            start: The range start datetime.
            end: The range end datetime.

        Returns:
            AggregateStatsRow with summary statistics.
        """
        result = self._analytics_repo.aggregate_stats(start.isoformat(), end.isoformat())
        if result is None:
            return AggregateStatsRow()
        return AggregateStatsRow(
            total_credits_consumed=_dec(result.total_credits_consumed),
            active_users=int(result.active_users),
            avg_daily_spend=_dec(result.avg_daily_spend),
            top_model=str(result.top_model),
            top_user=str(result.top_user),
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
        """List credit transactions for a user with optional filters.

        Args:
            user_id: The user ID.
            types: Filter by transaction types, or None for all.
            from_date: Start of date range, or None.
            to_date: End of date range, or None.
            limit: Maximum number of rows to return (default 50).
            offset: Number of rows to skip (default 0).

        Returns:
            List of TransactionRow (may be empty).
        """
        rows = self._analytics_repo.list_user_transactions(
            user_id,
            types,
            from_date.isoformat() if from_date else None,
            to_date.isoformat() if to_date else None,
            limit,
            offset,
        )
        return [
            TransactionRow(
                id=str(r.id),
                user_id=str(r.user_id),
                amount=_dec(r.amount),
                type=str(r.type),
                reference_type=r.reference_type,
                reference_id=r.reference_id,
                metadata=r.metadata,
                created_at=str(r.created_at),
                total_count=int(r.total_count),
            )
            for r in rows
        ]

    def list_user_transactions_cursor(
        self,
        user_id: str,
        types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: tuple[datetime | str, str] | None = None,
    ) -> tuple[list[TransactionRow], tuple[str, str] | None]:
        cursor_created_at = None if cursor is None else str(cursor[0])
        cursor_id = None if cursor is None else cursor[1]
        rows = self._analytics_repo.list_transactions_cursor(
            user_id,
            types,
            from_date.isoformat() if from_date else None,
            to_date.isoformat() if to_date else None,
            limit,
            cursor_created_at,
            cursor_id,
        )
        transactions = [
            TransactionRow(
                id=str(r.id),
                user_id=str(r.user_id),
                amount=_dec(r.amount),
                type=str(r.type),
                reference_type=r.reference_type,
                reference_id=r.reference_id,
                metadata=r.metadata,
                created_at=str(r.created_at),
                total_count=0,
            )
            for r in rows
        ]
        marker = rows[-1] if rows else None
        next_cursor = (
            (str(marker.next_cursor_created_at), str(marker.next_cursor_id))
            if marker and marker.next_cursor_created_at is not None and marker.next_cursor_id is not None
            else None
        )
        return transactions, next_cursor

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: Decimal = Decimal(0)) -> CreateTeamResult:
        """Create a new team with an initial credit balance.

        Args:
            name: The team name.
            initial_balance: The initial credit balance (default 0).

        Returns:
            CreateTeamResult with team_id and name.

        Raises:
            StoreError: If the RPC returns no result.
        """
        result = self._team_repo.create_team(name, str(_dec(initial_balance)))
        if result is None:
            raise StoreError("create_team returned no result")
        return CreateTeamResult(
            team_id=str(getattr(result, "team_id", "")),
            name=str(getattr(result, "name", name)),
        )

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        """Get the credit balance and member count for a team.

        Args:
            team_id: The team ID.

        Returns:
            TeamBalanceResult with balance details or defaults if team not found.
        """
        result = self._team_repo.get_team_balance(team_id)
        if result is None:
            return TeamBalanceResult(team_id=team_id)
        if result.error is not None:
            raise StoreError(f"get_team_balance failed: {result.error}")
        return TeamBalanceResult(
            team_id=str(getattr(result, "team_id", team_id)),
            name=str(getattr(result, "name", "")),
            balance=_dec(result.balance),
            member_count=int(getattr(result, "member_count", 0)),
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: Decimal | None = None,
    ) -> AddTeamMemberResult:
        """Add a member to a team with an optional spend cap.

        Args:
            team_id: The team ID.
            user_id: The user ID to add.
            role: The member role (default "member").
            spend_cap: The spend cap, or None for unlimited.

        Returns:
            AddTeamMemberResult with team_id, user_id, and role.

        Raises:
            StoreError: If the RPC returns no result.
        """
        result = self._team_repo.add_team_member(
            team_id,
            user_id,
            role,
            str(_dec(spend_cap)) if spend_cap is not None else None,
        )
        if result is None:
            raise StoreError("add_team_member returned no result")
        return AddTeamMemberResult(
            team_id=str(getattr(result, "team_id", team_id)),
            user_id=str(getattr(result, "user_id", user_id)),
            role=str(getattr(result, "role", role)),
        )

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        """Get all members of a team.

        Args:
            team_id: The team ID.

        Returns:
            List of TeamMember (may be empty).
        """
        rows = self._team_repo.get_team_members(team_id)
        return [
            TeamMember(
                user_id=str(r.user_id),
                role=str(r.role),
                spend_cap=_dec(r.spend_cap) if r.spend_cap is not None else None,
                total_spent=_dec(r.total_spent),
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
        """Deduct credits from a team's balance on behalf of a member.

        Args:
            team_id: The team ID.
            user_id: The user ID making the deduction.
            amount: The amount to deduct.
            metadata: Optional structured metadata.
            idempotency_key: Idempotency key threaded through metadata.

        Returns:
            TeamDeductionResult with transaction details.

        Raises:
            StoreError: If the RPC returns no result.
        """
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        # Thread the idempotency key through metadata (the RPC reads it from
        # metadata->>'idempotency_key') for idempotent replay (H12).
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key
        result = self._team_repo.deduct_team(team_id, user_id, str(amount), json.dumps(meta))
        if result is None:
            raise StoreError("deduct_team returned no result")
        if result.error is not None:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=Decimal(0),
                team_balance_after=_dec(result.team_balance_after),
                error=str(result.error),
            )
        return TeamDeductionResult(
            transaction_id=str(getattr(result, "transaction_id", "")),
            team_id=str(getattr(result, "team_id", team_id)),
            user_id=str(getattr(result, "user_id", user_id)),
            amount=_dec(result.amount, -amount),
            team_balance_after=_dec(result.team_balance_after),
        )

    # ── Credit expiry ───────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False, user_id: str | None = None) -> SweepResult:
        """Sweep (expire) credits that have passed their expiry date.

        Args:
            dry_run: If True, report what would be expired without modifying data.
            user_id: If set, only sweep credits for this user; otherwise sweep all.

        Returns:
            SweepResult with expiry counts and amounts.
        """
        result = self._bucket_repo.sweep_expired_credits(dry_run, user_id)
        if result is None:
            return SweepResult(expired_count=0, expired_amount=Decimal(0), dry_run=dry_run)
        return SweepResult(
            expired_count=int(getattr(result, "expired_count", 0)),
            expired_amount=_dec(result.expired_amount),
            dry_run=dry_run,
            expired_by_bucket=_dec_map(result.expired_by_bucket),
        )

    # ── Credit buckets ────────────────────────────────────────────────

    def get_bucket_balances(self, user_id: str) -> BucketBalancesResult:
        """Get all credit bucket balances for a user.

        Args:
            user_id: The user ID.

        Returns:
            BucketBalancesResult with list of bucket balances and total.
        """
        result = self._bucket_repo.get_bucket_balances(user_id)
        if result is None:
            return BucketBalancesResult(user_id=user_id, buckets=[], total_balance=Decimal(0))
        buckets = [
            BucketBalance(
                bucket_key=str(t.get("bucket_key", "")),
                label=str(t.get("name", "")),
                priority=int(t.get("priority", 0)),
                expires=bool(t.get("expires", False)),
                balance=_dec(t.get("balance")),
            )
            for t in (result.buckets or [])
        ]
        return BucketBalancesResult(
            user_id=str(getattr(result, "user_id", user_id)),
            buckets=buckets,
            total_balance=_dec(result.total_balance),
        )


def run_migrations(database_url: str) -> SetupResult:
    """Run bundled SQL migrations against *database_url*.

    Standalone entry point for the CLI ``migrate`` command.
    """
    store = PostgresStore(database_url)
    return store.setup(database_url)


def stamp_migrations(database_url: str) -> SetupResult:
    """Stamp the current HEAD migrations into the ledger without executing them.

    Standalone entry point for the CLI ``migrate --baseline-head`` command.
    """
    store = PostgresStore(database_url)
    return store.stamp()
