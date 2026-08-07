"""Vanilla PostgreSQL-backed credit store adapter.

Connects directly via ``psycopg2`` to any compatible PostgreSQL database with
the Bursar schema installed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import cached_property
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import psycopg2

from bursar.credits.postgres.repositories.analytics import AnalyticsRepository
from bursar.credits.postgres.repositories.balance import BalanceRepository
from bursar.credits.postgres.repositories.bucket import BucketRepository
from bursar.credits.postgres.repositories.catalog import CatalogRepository
from bursar.credits.postgres.repositories.deduction import DeductionRepository
from bursar.credits.postgres.repositories.lease import LeaseRepository
from bursar.credits.postgres.repositories.plan import PlanRepository
from bursar.credits.postgres.repositories.schemas import (
    CreateLeaseParams,
    DeductParams,
    SettleLeaseParams,
)
from bursar.credits.postgres.repositories.team import TeamRepository
from bursar.credits.store import (
    CreateLeaseOptions,
    CreditStore,
    SettleLeaseOptions,
    StoreError,
)
from bursar.credits.types import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStats,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalance,
    BucketBalancesResult,
    CatalogRevision,
    CatalogRevisionSummary,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    Entitlement,
    ExecuteGrantProgramRequest,
    GetUserPlanResult,
    GrantProgramAwardResult,
    LeasePricingContext,
    LeaseResult,
    LedgerCursor,
    LedgerEntry,
    LedgerPage,
    ListQuotaEventsOptions,
    PlanAdmissionPolicy,
    PlanAllowancePolicy,
    PlanCreditPolicy,
    PlanMigrationBatchResult,
    PlanMigrationStartResult,
    QuotaEvent,
    QuotaState,
    RefundResult,
    ReleaseResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TopUserRow,
    UsageCharge,
    UsageChargeCursor,
    UsageChargePage,
    UsageRecordResult,
)
from bursar.errors import BursarError
from bursar.shared.postgres_client import PostgresClient, PostgresConnectionOptions, PostgresPool
from bursar.sql import _get_sql_files


def _dec(value: Any) -> Decimal:
    """Coerce a NUMERIC or JSON value to ``Decimal``.

    psycopg2 already returns NUMERIC columns as ``Decimal``; this guards the
    ``int``/``str`` cases (and a stray ``float``, routed through ``str`` to avoid
    binary-float error) so no money value is ever truncated via ``int``. Missing,
    boolean, NaN, and infinite values are rejected instead of becoming zero.
    """
    if value is None or isinstance(value, bool):
        raise StoreError(f"PostgreSQL returned a missing or invalid Decimal value: {value!r}")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
        except (InvalidOperation, ArithmeticError, TypeError, ValueError) as e:
            raise StoreError(f"Failed to parse Decimal value: {value!r}") from e
    if not parsed.is_finite():
        raise StoreError(f"Decimal value must be finite: {value!r}")
    return parsed


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _require_text(value: Any, context: str) -> str:
    text = _text(value)
    if not text:
        raise StoreError(f"{context} returned a missing or invalid identifier")
    return text


def _dec_map(value: Any) -> dict[str, Decimal] | None:
    """Coerce a ``{bucket_key: amount}`` JSONB object into ``dict[str, Decimal]``.

    Used for ``bucket_breakdown``/``expired_by_bucket`` fields, which come back from
    RPCs as a JSON object of tier key -> NUMERIC amount. Returns ``None`` for a
    missing/empty/non-dict value so callers can distinguish "no tier data" from
    "empty breakdown" the same way the rest of this module treats optional fields.
    """
    if not value or not isinstance(value, dict):
        return None
    return {str(k): _dec(v) for k, v in value.items()}


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

    def __init__(
        self,
        database_url: str | None = None,
        *,
        tenant_id: str | UUID,
        max_pool_size: int = 20,
        pool: PostgresPool | None = None,
        usage_backend: Literal["postgres", "clickhouse"] = "postgres",
        connection_timeout_seconds: float = 10.0,
        statement_timeout_ms: int = 30_000,
        idle_transaction_timeout_ms: int = 30_000,
        application_name: str = "bursar-python",
        on_pool_error: Callable[[BursarError], None] | None = None,
        postgres_options: PostgresConnectionOptions | None = None,
    ) -> None:
        super().__init__()
        if pool is None and (not isinstance(database_url, str) or not database_url.strip()):
            raise ValueError("database_url is required when pool is not provided")
        if pool is not None and database_url is not None:
            raise ValueError("provide either database_url or pool, not both")
        if pool is None:
            assert database_url is not None
        self._database_url = database_url
        self._tenant_id = str(UUID(str(tenant_id)))
        self._usage_backend = usage_backend
        self._client = (
            PostgresClient.from_pool(
                pool,
                tenant_id=self._tenant_id,
                usage_backend=usage_backend,
                connection_timeout_seconds=connection_timeout_seconds,
                statement_timeout_ms=statement_timeout_ms,
                idle_transaction_timeout_ms=idle_transaction_timeout_ms,
                application_name=application_name,
                on_pool_error=on_pool_error,
                postgres_options=postgres_options,
            )
            if pool is not None
            else PostgresClient(
                cast(str, database_url),
                max_connections=max_pool_size,
                tenant_id=self._tenant_id,
                usage_backend=usage_backend,
                connection_timeout_seconds=connection_timeout_seconds,
                statement_timeout_ms=statement_timeout_ms,
                idle_transaction_timeout_ms=idle_transaction_timeout_ms,
                application_name=application_name,
                on_pool_error=on_pool_error,
                postgres_options=postgres_options,
            )
        )

    @property
    def database_url(self) -> str:
        """Postgres connection string for this store (read-only)."""
        if self._database_url is None:
            raise RuntimeError("this PostgresStore uses an application-owned pool")
        return self._database_url

    @property
    def tenant_id(self) -> str:
        """Tenant UUID bound to every store transaction."""
        return self._tenant_id

    def _bind_tenant(self, cursor: Any) -> None:
        if self._tenant_id is None:
            raise RuntimeError("tenant_id is required for Bursar store operations")
        cursor.execute(
            "SELECT set_config('bursar.tenant_id', %s, true)",
            (self._tenant_id,),
        )
        cursor.execute(
            "SELECT set_config('bursar.usage_backend', %s, true)",
            (self._usage_backend,),
        )

    # ── Repository getters ─────────────────────────────────────────────
    @cached_property
    def _balance_repo(self) -> BalanceRepository:
        return BalanceRepository(self._callproc)

    @cached_property
    def _deduction_repo(self) -> DeductionRepository:
        return DeductionRepository(self._callproc, self._query)

    @cached_property
    def _lease_repo(self) -> LeaseRepository:
        return LeaseRepository(self._callproc)

    @cached_property
    def _catalog_repo(self) -> CatalogRepository:
        return CatalogRepository(self._callproc)

    @cached_property
    def _plan_repo(self) -> PlanRepository:
        return PlanRepository(self._callproc)

    @cached_property
    def _analytics_repo(self) -> AnalyticsRepository:
        return AnalyticsRepository(self._callproc)

    @cached_property
    def _team_repo(self) -> TeamRepository:
        return TeamRepository(self._callproc)

    @cached_property
    def _bucket_repo(self) -> BucketRepository:
        return BucketRepository(self._callproc)

    def close(self) -> None:
        """Close all connections in the pool."""
        self._client.close()

    def __enter__(self) -> PostgresStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    # ── RPC dispatcher ─────────────────────────────────────────────────

    def _callproc(self, name: str, params: list[Any]) -> list[Any]:
        """Execute an RPC and return all result rows, using the connection pool.

        For single-column results (e.g. JSONB functions), each row is unwrapped
        to its scalar value. Multi-column TABLE functions are returned as
        dictionaries keyed by their declared column names.
        """
        return self._client.callproc(name, params)

    def _query(self, sql: str, params: list[Any]) -> list[Any]:
        return self._client.query(sql, params)

    # ── Schema management ──────────────────────────────────────────────

    @staticmethod
    def _migrate(
        database_url: str,
        *,
        post_migration_sql: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Apply bundled migrations exactly once, transactionally.

        A migration ledger records each filename and SHA-256 checksum. An
        advisory transaction lock serializes concurrent deploys, and any
        failed migration aborts the setup transaction. Trusted host SQL runs
        after the bundled files, in the supplied order and in the same
        transaction, but is not recorded in Bursar's migration ledger.
        """
        try:
            conn = psycopg2.connect(
                database_url,
                connect_timeout=10,
                application_name="bursar-python",
            )
        except psycopg2.Error as error:
            raise StoreError(f"database connection failed: {error}") from error
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

                for source, sql in post_migration_sql:
                    try:
                        cur.execute(sql)
                    except psycopg2.Error as exc:
                        raise StoreError(f"post-migration SQL failed for {source}: {exc}") from exc
            conn.commit()
        except StoreError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise StoreError(f"Bursar setup failed transactionally: {exc}") from exc
        finally:
            conn.close()

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
            return BalanceResult(user_id=user_id, balance=Decimal(0), lifetime_purchased=Decimal(0))

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
            AddCreditsResult with ledger entry details.

        Raises:
            StoreError: If the RPC returns no result or an error.
        """
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json") if metadata else {}
        if expires_at:
            meta["expires_at"] = expires_at.isoformat()
        effective_idempotency_key = idempotency_key or f"credit:{uuid4()}"
        result = self._balance_repo.add_credits(
            user_id,
            str(amount),
            type,
            json.dumps(meta),
            expires_at.isoformat() if expires_at else None,
            bucket,
            effective_idempotency_key,
        )
        if result is None:
            raise StoreError("credits_add returned no result")
        if result.error is not None:
            raise StoreError(f"post_credit failed: {result.error}")
        return AddCreditsResult(
            entry_id=_require_text(result.entry_id, "post_credit"),
            user_id=str(getattr(result, "user_id", user_id)),
            amount=_dec(result.amount),
            new_balance=_dec(result.new_balance),
            lifetime_purchased=_dec(result.lifetime_purchased),
            bucket=str(getattr(result, "bucket", "default")),
            idempotent=bool(getattr(result, "idempotent", False)),
        )

    def deduct_with_allowance(
        self,
        user_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        operation: str = "usage",
        feature: str | None = None,
        model: str | None = None,
        region: str | None = None,
        measures: dict[str, Decimal | int | float] | None = None,
        dimensions: dict[str, Any] | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        """Call the plan-aware atomic usage-charge RPC."""
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        effective_idempotency_key = idempotency_key or f"usage:{uuid4()}"

        params = DeductParams(
            user_id=user_id,
            operation=operation,
            amount=str(amount),
            idempotency_key=effective_idempotency_key,
            feature=feature,
            model=model,
            region=region,
            measures=json.dumps(measures or {}, cls=DecimalEncoder),
            dimensions=json.dumps(dimensions or {}, cls=DecimalEncoder),
            metadata=json.dumps(meta, cls=DecimalEncoder),
        )
        result = self._deduction_repo.deduct_with_allowance(params)

        if result is None:
            raise StoreError("charge_usage_for_operation returned no result")
        if result.error is not None:
            return DeductionResult(
                entry_id=None,
                user_id=user_id,
                amount=_dec(result.amount),
                allowance_consumed=_dec(result.allowance_consumed),
                balance_after=_dec(result.balance_after) if result.balance_after is not None else None,
                idempotent=False,
                error=str(result.error),
            )

        return DeductionResult(
            entry_id=(
                _require_text(result.entry_id, "charge_usage_for_operation") if result.entry_id is not None else None
            ),
            usage_charge_id=_require_text(result.charge_id, "charge_usage_for_operation"),
            user_id=user_id,
            amount=_dec(result.amount),
            allowance_consumed=_dec(result.allowance_consumed),
            balance_after=_dec(result.balance_after),
            idempotent=bool(getattr(result, "idempotent", False)),
            bucket_breakdown=_dec_map(result.bucket_breakdown),
        )

    def record_usage(
        self,
        user_id: str,
        operation: str,
        requested: Decimal,
        *,
        idempotency_key: str | None = None,
        feature: str | None = None,
        model: str | None = None,
        region: str | None = None,
        measures: dict[str, Decimal | int | float] | None = None,
        dimensions: dict[str, Any] | None = None,
        metadata: CreditMetadata | None = None,
    ) -> UsageRecordResult:
        """Append priced usage telemetry without creating another debit."""
        requested = _dec(requested)
        effective_idempotency_key = idempotency_key or f"usage-record:{uuid4()}"
        params = DeductParams(
            user_id=user_id,
            operation=operation,
            amount=str(requested),
            idempotency_key=effective_idempotency_key,
            feature=feature,
            model=model,
            region=region,
            measures=json.dumps(measures or {}, cls=DecimalEncoder),
            dimensions=json.dumps(dimensions or {}, cls=DecimalEncoder),
            metadata=json.dumps(
                metadata.model_dump(mode="json", exclude_none=True) if metadata else {},
                cls=DecimalEncoder,
            ),
        )
        result = self._deduction_repo.record_usage(params)
        if result is None:
            raise StoreError("record_usage returned no result")
        if result.error_code is not None:
            return UsageRecordResult(
                usage_id=None,
                user_id=user_id,
                requested=_dec(result.requested),
                idempotent=False,
                error=result.error_code,
            )
        return UsageRecordResult(
            usage_id=_require_text(result.charge_id, "record_usage"),
            user_id=user_id,
            requested=_dec(result.requested),
            idempotent=bool(result.replayed),
            error=None,
        )

    # ── Lease lifecycle (atomic admission) ─────────────────────────────

    def create_lease(
        self,
        user_id: str,
        amount: Decimal,
        operation_type: str,
        options: CreateLeaseOptions | None = None,
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
            feature: Entitlement feature key, or None.

        Returns:
            LeaseResult with lease_id and reservation details.

        Raises:
            StoreError: If the RPC returns no result (admission denied).
        """
        options = options or CreateLeaseOptions()
        amount = _dec(amount)
        floor = _dec(options.floor)
        effective_idempotency_key = options.idempotency_key or f"lease:{uuid4()}"
        effective_dimensions = dict(options.dimensions or {})
        if options.model is not None:
            effective_dimensions.setdefault("model", options.model)
        if options.region is not None:
            effective_dimensions.setdefault("region", options.region)

        params = CreateLeaseParams(
            user_id=user_id,
            amount=str(amount),
            operation_type=operation_type,
            idempotency_key=effective_idempotency_key,
            ttl_seconds=options.ttl_seconds,
            metadata=json.dumps(
                options.metadata.model_dump(mode="json", exclude_none=True) if options.metadata else {},
                cls=DecimalEncoder,
            ),
            feature=options.feature,
            measures=json.dumps(options.measures or {}, cls=DecimalEncoder),
            dimensions=json.dumps(effective_dimensions, cls=DecimalEncoder),
            minimum_balance=str(floor),
            max_concurrent=options.max_concurrent,
        )
        result = self._lease_repo.create_lease(params)
        if result is None:
            raise StoreError("create_lease_for_operation returned no result")
        availability = self.get_available(user_id)
        if result.error is not None:
            return LeaseResult(
                lease_id=None,
                user_id=user_id,
                amount=_dec(result.amount) if result.amount is not None else None,
                available=availability.available,
                reserved_total=availability.reserved,
                minimum_balance=(_dec(result.minimum_balance) if result.minimum_balance is not None else None),
                billing_mode=options.billing_mode,
                expires_at=None,
                error=str(result.error),
            )
        minimum_balance = _dec(result.minimum_balance)
        return LeaseResult(
            lease_id=_require_text(result.lease_id, "create_lease_for_operation"),
            user_id=user_id,
            amount=_dec(result.amount),
            available=availability.available,
            reserved_total=availability.reserved,
            minimum_balance=minimum_balance,
            billing_mode="overdraft" if minimum_balance < 0 else "strict",
            expires_at=_require_text(result.expires_at, "create_lease_for_operation"),
            error=None,
        )

    def settle_lease(
        self,
        user_id: str,
        lease_id: str,
        amount: Decimal,
        options: SettleLeaseOptions | None = None,
    ) -> DeductionResult:
        """Settle a lease by deducting the actual amount used.

        Args:
            user_id: The user ID.
            lease_id: The lease ID to settle.
            amount: The actual amount to charge.
            idempotency_key: Idempotency key for replay protection.
            floor: Minimum balance floor after deduction.
            model: The AI model identifier, or None.
            metadata: Optional structured metadata.
            skip_allowance: If True, skip plan allowance checks.
            period_start: Calendar period start date, or None.
            feature: Entitlement feature key, or None.

        Returns:
            DeductionResult with ledger entry details.
        """
        options = options or SettleLeaseOptions()
        amount = _dec(amount)
        meta = options.metadata.model_dump(mode="json", exclude_none=True) if options.metadata else {}
        effective_idempotency_key = options.idempotency_key or f"lease:{lease_id}:settle"
        effective_dimensions = dict(options.dimensions or {})
        if options.model is not None:
            effective_dimensions.setdefault("model", options.model)
        if options.region is not None:
            effective_dimensions.setdefault("region", options.region)

        params = SettleLeaseParams(
            user_id=user_id,
            lease_id=lease_id,
            amount=str(amount),
            idempotency_key=effective_idempotency_key,
            model=options.model,
            feature=options.feature,
            region=options.region,
            measures=json.dumps(options.measures or {}, cls=DecimalEncoder),
            dimensions=json.dumps(effective_dimensions, cls=DecimalEncoder),
            metadata=json.dumps(meta, cls=DecimalEncoder),
        )
        result = self._lease_repo.settle_lease(params)

        if result is None:
            raise StoreError("settle_lease returned no result")
        if result.error is not None:
            return DeductionResult(
                entry_id=None,
                user_id=user_id,
                amount=_dec(result.amount),
                allowance_consumed=Decimal(0),
                balance_after=_dec(result.balance_after) if result.balance_after is not None else None,
                idempotent=False,
                error=str(result.error),
            )
        return DeductionResult(
            entry_id=_require_text(result.entry_id, "settle_lease") if result.entry_id is not None else None,
            usage_charge_id=_require_text(result.charge_id, "settle_lease"),
            user_id=user_id,
            amount=_dec(result.amount),
            allowance_consumed=_dec(result.allowance_consumed),
            balance_after=_dec(result.balance_after),
            idempotent=bool(getattr(result, "idempotent", False)),
            bucket_breakdown=_dec_map(result.bucket_breakdown),
        )

    def get_lease_pricing_context(self, user_id: str, lease_id: str) -> LeasePricingContext | None:
        """Read the immutable pricing context captured by a subject-owned lease."""
        result = self._lease_repo.get_pricing_context(user_id, lease_id)
        if result is None:
            return None
        return LeasePricingContext(
            catalog_version=result.catalog_revision_no,
            plan_id=result.plan_id,
            plan_key=result.plan_key,
            rate_card=result.rate_card,
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
            raise StoreError("release_lease returned no result")
        return ReleaseResult(
            lease_id=lease_id,
            user_id=user_id,
            released=bool(getattr(result, "released", False)),
            reason=result.reason,
        )

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        """Extend an active lease's expiry without changing its policy snapshot."""
        result = self._lease_repo.renew_lease(user_id, lease_id, ttl_seconds)
        availability = self.get_available(user_id)
        if result is None:
            raise StoreError("renew_lease returned no result")
        if result.error is not None:
            return LeaseResult(
                lease_id=None,
                user_id=user_id,
                amount=_dec(result.amount) if result.amount is not None else None,
                available=availability.available,
                reserved_total=availability.reserved,
                minimum_balance=(_dec(result.minimum_balance) if result.minimum_balance is not None else None),
                billing_mode=(
                    "overdraft" if result.minimum_balance is not None and _dec(result.minimum_balance) < 0 else "strict"
                ),
                expires_at=None,
                error=result.error,
            )
        minimum_balance = _dec(result.minimum_balance)
        return LeaseResult(
            lease_id=_require_text(result.lease_id, "renew_lease"),
            user_id=user_id,
            amount=_dec(result.amount),
            available=availability.available,
            reserved_total=availability.reserved,
            minimum_balance=minimum_balance,
            billing_mode="overdraft" if minimum_balance < 0 else "strict",
            expires_at=_require_text(result.expires_at, "renew_lease"),
            error=None,
        )

    def expire_leases(self, limit: int = 100) -> int:
        """Expire a bounded batch of abandoned leases and release reservations."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("lease expiry limit must be an integer between 1 and 1000")
        return self._lease_repo.expire_leases(limit)

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

    # ── Catalog configuration ──────────────────────────────────────────

    def get_active_catalog(self) -> CatalogRevision | None:
        return self._load_active_catalog()

    def _normalize_catalog_revision(self, result: Any) -> CatalogRevision | None:
        """Normalize a raw catalog revision into CatalogRevision."""
        if result is None:
            return None
        return CatalogRevision.model_validate(result.model_dump())

    def _load_active_catalog(self) -> CatalogRevision | None:
        return self._normalize_catalog_revision(self._catalog_repo.get_active_catalog())

    def publish_and_activate_catalog(
        self,
        config: dict[str, Any],
        label: str | None = None,
        rollout: Any | None = None,
    ) -> str:
        """Publish and activate a catalog revision.

        Args:
            config: The Bursar configuration document.
            label: Optional human-readable label.

        Returns:
            The ID of the newly activated catalog revision.

        Raises:
            StoreError: If the RPC returns no result.
            ConfigError: If the config fails validation.
        """
        from bursar.config import (
            canonical_bursar_config_dict,
            canonical_catalog_rollout_dict,
            load_config_from_dict,
        )

        canonical = canonical_bursar_config_dict(config)
        parsed = load_config_from_dict(canonical)
        rollout_document = canonical_catalog_rollout_dict(rollout, parsed)
        result = self._catalog_repo.publish_and_activate_catalog(
            json.dumps(canonical, cls=DecimalEncoder),
            label,
            rollout_document,
        )
        if result is None:
            raise StoreError("publish_and_activate_catalog returned no result")
        return result.id

    def get_catalog_history(self) -> list[CatalogRevisionSummary]:
        """Get all catalog revisions.

        Returns:
            List of CatalogRevisionSummary (may be empty).
        """
        rows = self._catalog_repo.get_catalog_history()
        return [
            CatalogRevisionSummary(
                id=str(r.id),
                version=r.version,
                label=r.label,
                active=r.active,
                created_at=str(r.created_at),
            )
            for r in rows
        ]

    def get_catalog_revision(self, version: int) -> CatalogRevision | None:
        """Get a catalog revision by version number.

        Args:
            version: The version number to retrieve.

        Returns:
            CatalogRevision if found, None otherwise.
        """
        return self._normalize_catalog_revision(self._catalog_repo.get_catalog_revision(version))

    def activate_catalog_revision(self, version: int, rollout: Any | None = None) -> str:
        """Activate a catalog revision.

        Args:
            version: The version number to activate.

        Returns:
            The ID of the activated config.

        Raises:
            StoreError: If the version is not found.
        """
        from bursar.config import (
            canonical_catalog_rollout_dict,
            load_config_from_dict,
        )

        target = self.get_catalog_revision(version)
        target_config = load_config_from_dict(target.config) if target is not None else None
        rollout_document = canonical_catalog_rollout_dict(
            rollout,
            target_config,
        )
        result = self._catalog_repo.activate_catalog_revision(
            version,
            rollout_document,
        )
        if result is None:
            msg = f"Version {version} not found"
            raise StoreError(msg)
        return result.id

    def publish_catalog_draft(
        self,
        config: dict[str, Any],
        label: str | None = None,
    ) -> str:
        """Publish an inactive catalog draft."""
        from bursar.config import canonical_bursar_config_dict

        canonical = canonical_bursar_config_dict(config)
        result = self._catalog_repo.publish_catalog_draft(json.dumps(canonical, cls=DecimalEncoder), label)
        if result is None:
            raise StoreError("publish_catalog_draft returned no result")
        return result.id

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
            return GetUserPlanResult(
                user_id=user_id,
                plan_id=None,
                plan_key=None,
                plan_label=None,
                allowance=None,
                entitlements={},
                credit_policy=None,
                admission=None,
                allowed_operations=[],
            )
        allowance = None
        if result.credit_allowance_amount is not None:
            if (
                result.credit_allowance_priority is None
                or result.credit_allowance_reset_unit is None
                or result.credit_allowance_reset_count is None
                or result.credit_allowance_reset_anchor is None
                or result.credit_allowance_reset_timezone is None
            ):
                raise StoreError("get_user_plan returned an incomplete allowance policy")
            allowance = PlanAllowancePolicy(
                amount=_dec(result.credit_allowance_amount),
                priority=result.credit_allowance_priority,
                reset_unit=result.credit_allowance_reset_unit,
                reset_count=result.credit_allowance_reset_count,
                reset_anchor=result.credit_allowance_reset_anchor,
                reset_timezone=result.credit_allowance_reset_timezone,
            )
        admission_operations = {
            str(operation): {"max_in_flight": policy.get("max_in_flight") if isinstance(policy, dict) else None}
            for operation, policy in (result.operation_admission or {}).items()
        }
        plan_assigned_at = result.plan_assigned_at
        if plan_assigned_at is not None and not isinstance(plan_assigned_at, datetime):
            plan_assigned_at = datetime.fromisoformat(str(plan_assigned_at))
        return GetUserPlanResult(
            user_id=str(getattr(result, "user_id", user_id)),
            plan_id=result.plan_id or None,
            plan_key=result.plan_key or None,
            plan_label=result.plan_label or None,
            allowance=allowance,
            entitlements={k: Entitlement.model_validate(v) for k, v in (result.entitlements or {}).items()},
            rate_card=result.rate_card,
            credit_policy=(
                PlanCreditPolicy.model_validate(
                    {
                        "type": result.credit_policy_type,
                        "credit_limit": _dec(result.credit_limit) if result.credit_limit is not None else None,
                    }
                )
                if result.credit_policy_type is not None
                else None
            ),
            admission=(
                PlanAdmissionPolicy.model_validate(
                    {
                        "max_in_flight": result.admission_max_in_flight,
                        "operations": admission_operations,
                    }
                )
                if result.admission_max_in_flight is not None or admission_operations
                else None
            ),
            allowed_operations=list(result.allowed_operations or []),
            plan_assigned_at=plan_assigned_at,
            assignment_source_type=result.assignment_source_type,
            assignment_source_id=result.assignment_source_id,
            catalog_revision_pinned=result.catalog_revision_pinned,
            catalog_version=result.catalog_revision_no,
        )

    def check_feature(self, user_id: str, feature: str) -> CheckFeatureResult:
        entitlement = self._plan_repo.get_entitlement(user_id, feature)
        value = entitlement.get("feature_value") if entitlement is not None else None
        return CheckFeatureResult(
            user_id=user_id,
            feature=feature,
            value=value,
            has_feature=entitlement is not None and value is not None and value is not False,
        )

    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult:
        """Assign a plan to a user.

        Args:
            user_id: The user ID.
            plan_key: The public plan key.
            plan_assigned_at: The assignment datetime, or None for now.

        Returns:
            SetUserPlanResult with assignment details.

        Raises:
            StoreError: If the RPC returns no result.
        """
        result = self._plan_repo.set_user_plan(
            user_id,
            plan_key,
            plan_assigned_at.isoformat() if plan_assigned_at else None,
        )
        if result is None:
            raise StoreError("set_user_plan returned no result")
        return SetUserPlanResult(
            user_id=str(getattr(result, "user_id", user_id)),
            plan_id=str(result.plan_id),
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

    def set_plan_revision_pin(self, user_id: str, pinned: bool) -> bool:
        """Pin or unpin the user's current catalog-plan revision."""
        return self._plan_repo.set_plan_revision_pin(user_id, pinned)

    def apply_due_plan_changes(self, limit: int = 100) -> int:
        """Apply one bounded batch of renewal-effective plan changes."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("plan change limit must be an integer between 1 and 1000")
        return self._plan_repo.apply_due_plan_changes(limit)

    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> PlanMigrationStartResult:
        migration_id = self._plan_repo.start_plan_migration(from_plan_id, to_plan_id)
        if migration_id is None:
            raise StoreError("start_plan_migration returned no migration id")
        return PlanMigrationStartResult(migration_id=migration_id)

    def migrate_plan_batch(
        self,
        migration_id: str,
        batch_size: int = 100,
    ) -> PlanMigrationBatchResult:
        result = self._plan_repo.migrate_plan_batch(migration_id, batch_size)
        if result is None:
            raise StoreError("migrate_plan_batch returned no data")
        return PlanMigrationBatchResult(
            migrated=result.migrated,
            done=result.done,
            next_cursor=result.next_cursor,
        )

    def get_quota_state(
        self,
        user_id: str,
        quota_key: str | None = None,
    ) -> list[QuotaState]:
        rows = self._plan_repo.get_quota_state(user_id, quota_key)
        return [
            QuotaState(
                user_id=row.user_id,
                quota_key=row.quota_key,
                operation=row.operation_key,
                measure=row.measure_key,
                limit=_dec(row.quota_limit),
                consumed=_dec(row.consumed),
                reserved=_dec(row.reserved),
                remaining=_dec(row.remaining),
                overage=_dec(row.overage),
                enforcement=row.enforcement,
                window_start=row.window_start,
                window_end=row.window_end,
                emit_at_percent=row.emit_at_percent,
            )
            for row in rows
        ]

    def check_allowance(self, user_id: str) -> AllowanceResult | None:
        """Check the remaining plan allowance for a user.

        Args:
            user_id: The user ID.
        Returns:
            AllowanceResult with the current window, or ``None`` when the
            subject has no active allowance policy.
        """
        result = self._plan_repo.check_allowance(user_id)
        if result is None:
            return None
        return AllowanceResult(
            plan_id=result.plan_id,
            allowance_remaining=_dec(result.allowance_remaining),
            period_start=_require_text(result.period_start, "get_subject_allowance"),
            period_end=_require_text(result.period_end, "get_subject_allowance"),
        )

    def list_quota_events(
        self,
        user_id: str,
        options: ListQuotaEventsOptions | None = None,
    ) -> list[QuotaEvent]:
        options = options or ListQuotaEventsOptions()
        limit = options.limit or 100
        if limit < 1 or limit > 500:
            raise ValueError("quota event limit must be between 1 and 500")
        if options.after_id is not None and options.after is None:
            raise ValueError("after_id requires after")
        rows = self._plan_repo.list_quota_events(
            user_id,
            options.after.isoformat() if options.after else None,
            limit,
            options.idempotency_key,
            options.after_id,
        )
        return [
            QuotaEvent(
                event_id=row.event_id,
                quota_key=row.quota_key,
                operation=row.operation_key,
                measure=row.measure_key,
                event_type=row.event_type,
                threshold_percent=row.threshold_percent,
                idempotency_key=row.idempotency_key,
                usage_charge_id=row.usage_charge_id,
                created_at=(row.created_at.isoformat() if isinstance(row.created_at, datetime) else row.created_at),
            )
            for row in rows
        ]

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        entry_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
        idempotency_key: str | None = None,
    ) -> RefundResult:
        """Refund a previous ledger entry.

        Args:
            entry_id: The original ledger entry ID to refund.
            amount: The amount to refund, or None for full refund.
            reason: The refund reason, or None.
            metadata: Optional structured metadata.

        Returns:
            RefundResult with refund ledger entry details.
        """
        result = self._deduction_repo.refund_credits(
            entry_id,
            str(_dec(amount)) if amount is not None else None,
            idempotency_key or f"refund:{entry_id}:{_dec(amount) if amount is not None else 'remaining'}",
            reason,
            json.dumps(
                metadata.model_dump(mode="json", exclude_none=True) if metadata else {},
                cls=DecimalEncoder,
            ),
        )
        if result is None:
            raise StoreError("refund_credit_by_entry returned no result")
        if result.error is not None:
            return RefundResult(
                refund_entry_id=None,
                original_entry_id=entry_id,
                user_id=result.user_id,
                amount=_dec(result.amount) if result.amount is not None else None,
                new_balance=_dec(result.new_balance) if result.new_balance is not None else None,
                error=str(result.error),
                bucket_breakdown=None,
            )
        return RefundResult(
            refund_entry_id=_require_text(result.refund_entry_id, "refund_credit_by_entry"),
            original_entry_id=entry_id,
            user_id=_require_text(result.user_id, "refund_credit_by_entry"),
            amount=_dec(result.amount),
            new_balance=_dec(result.new_balance),
            error=None,
            bucket_breakdown=_dec_map(result.bucket_breakdown),
        )

    def revoke_credits_by_entry_type(self, user_id: str, entry_type: str) -> dict:
        """Revoke credits for all transactions of a given type for a user.

        Args:
            user_id: The user ID.
            entry_type: The transaction type to revoke.

        Returns:
            Dict with user_id, amount, new_balance, and bucket.
        """
        result = self._deduction_repo.revoke_credits_by_entry_type(user_id, entry_type)
        if result is None:
            raise StoreError("revoke_subject_credits_by_operation returned no result")
        return {
            "user_id": str(getattr(result, "user_id", user_id)),
            "amount": str(_dec(result.amount)),
            "new_balance": str(_dec(result.new_balance)),
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
                entry_count=int(r.entry_count),
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
                entry_count=int(r.entry_count),
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
                entry_count=int(r.entry_count),
            )
            for r in rows
        ]

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats:
        """Get aggregate usage statistics for a date range.

        Args:
            start: The range start datetime.
            end: The range end datetime.

        Returns:
            AggregateStats with summary statistics.
        """
        result = self._analytics_repo.aggregate_stats(start.isoformat(), end.isoformat())
        return AggregateStats(
            total_credits_consumed=_dec(result.total_credits_consumed),
            active_users=int(result.active_users),
            avg_daily_spend=_dec(result.avg_daily_spend),
            top_model=str(result.top_model),
            top_user=str(result.top_user),
        )

    # ── Transaction listing ─────────────────────────────────────────────────

    @staticmethod
    def _ledger_entry(row: Any) -> LedgerEntry:
        return LedgerEntry(
            entry_id=str(row.entry_id),
            account_id=str(row.account_id),
            actor_user_id=str(row.actor_user_id) if row.actor_user_id else None,
            amount=_dec(row.amount),
            entry_type=str(row.entry_type),
            operation=str(row.operation),
            reference_entry_id=str(row.reference_entry_id) if row.reference_entry_id else None,
            idempotency_key=row.idempotency_key,
            metadata=row.metadata,
            created_at=str(row.created_at),
        )

    def _list_ledger_page(
        self,
        user_id: str,
        entry_types: list[str] | None,
        from_date: datetime | None,
        to_date: datetime | None,
        limit: int,
        cursor: LedgerCursor | None,
        *,
        usage_only: bool,
    ) -> LedgerPage:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        rows = self._analytics_repo.list_ledger_entries(
            user_id,
            entry_types,
            from_date.isoformat() if from_date else None,
            to_date.isoformat() if to_date else None,
            limit + 1,
            cursor.created_at if cursor else None,
            cursor.entry_id if cursor else None,
            usage_only=usage_only,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [self._ledger_entry(row) for row in visible]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = LedgerCursor(created_at=last.created_at, entry_id=last.entry_id)
        return LedgerPage(items=items, next_cursor=next_cursor)

    def list_ledger_entries(
        self,
        user_id: str,
        entry_types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage:
        return self._list_ledger_page(user_id, entry_types, from_date, to_date, limit, cursor, usage_only=False)

    def list_usage_entries(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage:
        return self._list_ledger_page(user_id, ["usage"], from_date, to_date, limit, cursor, usage_only=True)

    def list_usage_charges(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: UsageChargeCursor | None = None,
        include_record_only: bool = True,
    ) -> UsageChargePage:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        rows = self._analytics_repo.list_usage_charges(
            user_id,
            from_date.isoformat() if from_date else None,
            to_date.isoformat() if to_date else None,
            limit + 1,
            cursor.event_at if cursor else None,
            cursor.usage_id if cursor else None,
            include_record_only,
        )
        has_more = len(rows) > limit
        items = [
            UsageCharge(
                usage_id=str(row.usage_id),
                account_id=str(row.account_id),
                operation=str(row.operation),
                requested=_dec(row.requested),
                charged=_dec(row.charged),
                allowance_requested=_dec(row.allowance_requested),
                allowance_covered=_dec(row.allowance_covered),
                billing_disposition=row.billing_disposition,
                feature=row.feature,
                model=row.model,
                region=row.region,
                event_at=_text(row.event_at),
                idempotency_key=str(row.idempotency_key),
                metadata=row.metadata,
                created_at=_text(row.created_at),
            )
            for row in rows[:limit]
        ]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = UsageChargeCursor(event_at=last.event_at, usage_id=last.usage_id)
        return UsageChargePage(items=items, next_cursor=next_cursor)

    def get_ledger_entry(self, user_id: str, entry_id: str) -> LedgerEntry | None:
        row = self._analytics_repo.get_ledger_entry(user_id, entry_id)
        return self._ledger_entry(row) if row is not None else None

    def create_team(
        self,
        owner_subject_id: str,
        name: str,
        initial_balance: Decimal = Decimal(0),
    ) -> CreateTeamResult:
        """Create a new team with an initial credit balance.

        Args:
            owner_subject_id: Subject that owns the team.
            name: The team name.
            initial_balance: The initial credit balance (default 0).

        Returns:
            CreateTeamResult with team_id and name.

        Raises:
            StoreError: If the RPC returns no result.
        """
        result = self._team_repo.create_team(owner_subject_id, name, str(_dec(initial_balance)))
        if result is None:
            raise StoreError("create_team returned no result")
        if result.error_code is not None:
            raise StoreError(result.error_code)
        return CreateTeamResult(
            team_id=_require_text(result.team_id, "create_team"),
            name=name,
        )

    def get_team_balance(self, team_id: str) -> TeamBalanceResult | None:
        """Get the credit balance and member count for a team.

        Args:
            team_id: The team ID.

        Returns:
            TeamBalanceResult with balance details, or ``None`` if not found.
        """
        result = self._team_repo.get_team_balance(team_id)
        if result is None:
            return None
        return TeamBalanceResult(
            team_id=result.team_id,
            name=result.name,
            balance=_dec(result.balance),
            member_count=result.member_count,
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

    def remove_team_member(self, team_id: str, user_id: str) -> bool:
        """Remove a team member unless they are the final owner."""
        return self._team_repo.remove_team_member(team_id, user_id)

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
            TeamDeductionResult with ledger entry details.

        Raises:
            StoreError: If the RPC returns no result.
        """
        amount = _dec(amount)
        meta = metadata.model_dump(mode="json", exclude_none=True) if metadata else {}
        # Generate a default idempotency key when the caller supplies none, so two
        # otherwise-identical team charges are not collapsed into a single replay
        # (mirrors the JS store). deduct_team dedupes on the key argument.
        effective_key = idempotency_key or f"team-usage:{uuid4()}"
        meta["idempotency_key"] = effective_key
        operation_meta = meta.get("operation")
        operation = operation_meta if isinstance(operation_meta, str) and operation_meta else "team_usage"
        result = self._team_repo.deduct_team(
            team_id,
            user_id,
            str(amount),
            effective_key,
            operation,
            json.dumps(meta),
        )
        if result is None:
            raise StoreError("deduct_team returned no result")
        if result.error is not None:
            return TeamDeductionResult(
                entry_id=None,
                team_id=team_id,
                user_id=user_id,
                amount=_dec(result.amount),
                team_balance_after=(_dec(result.team_balance_after) if result.team_balance_after is not None else None),
                idempotent=False,
                error=str(result.error),
            )
        return TeamDeductionResult(
            entry_id=_require_text(result.entry_id, "deduct_team"),
            team_id=str(getattr(result, "team_id", team_id)),
            user_id=str(getattr(result, "user_id", user_id)),
            amount=_dec(result.amount),
            team_balance_after=_dec(result.team_balance_after),
            idempotent=result.replayed,
        )

    # ── Credit expiry ───────────────────────────────────────────────────

    def sweep_expired_credits(
        self,
        dry_run: bool = False,
        user_id: str | None = None,
        limit: int = 100,
    ) -> SweepResult:
        """Expire at most ``limit`` eligible credit lots."""
        result = self._bucket_repo.sweep_expired_credits(dry_run, user_id, limit)
        return SweepResult(
            expired_count=result.expired_count,
            expired_amount=_dec(result.expired_amount),
            dry_run=result.dry_run,
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

    def execute_grant_program(
        self,
        request: ExecuteGrantProgramRequest,
    ) -> list[GrantProgramAwardResult]:
        """Execute an application-driven catalog grant program."""
        metadata = request.metadata.model_dump(mode="json", exclude_none=True) if request.metadata else {}
        rows = self._balance_repo.execute_grant_program(
            request.trigger,
            request.program_key,
            request.subject_id,
            request.event_key,
            request.referrer_subject_id,
            request.region,
            json.dumps(metadata, cls=DecimalEncoder),
        )
        return [
            GrantProgramAwardResult(
                grant_event_id=row.grant_event_id,
                grant_award_id=row.grant_award_id,
                recipient_subject_id=row.recipient_subject_id,
                ledger_entry_id=row.ledger_entry_id,
                amount=_dec(row.amount),
                replayed=row.replayed,
                error=row.error_code,
            )
            for row in rows
        ]


def run_migrations(
    database_url: str,
    *,
    post_migration_sql: Sequence[tuple[str, str]] = (),
) -> None:
    """Run bundled SQL migrations against *database_url*.

    ``post_migration_sql`` contains trusted ``(source, SQL)`` pairs that run
    after Bursar's migrations, in order and in the same transaction. This is
    the host-integration hook used by the CLI's ``--post-migrate-sql`` option.
    """
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("database_url must not be empty")
    PostgresStore._migrate(database_url, post_migration_sql=post_migration_sql)
