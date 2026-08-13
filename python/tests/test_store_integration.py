"""PostgreSQL integration coverage for the public v1 Bursar configuration."""

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from bursar.credits.postgres.store import PostgresStore, run_migrations
from bursar.credits.service import CreditsService
from bursar.credits.service_types import ReserveOptions, SettleOptions
from bursar.credits.store import CreateLeaseOptions, StoreError
from bursar.credits.types import CreditMetadata, DeductionResult, ExecuteGrantProgramRequest, LeaseResult
from bursar.metrics import UsageMetrics
from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]
USER_ID = "00000000-0000-0000-0000-000000000901"
REPLAY_USER_ID = "00000000-0000-0000-0000-000000000911"
TEAM_REPLAY_OWNER_ID = "00000000-0000-0000-0000-000000000921"
TEAM_CONCURRENT_OWNER_ID = "00000000-0000-0000-0000-000000000922"
TEAM_CHANGED_OWNER_ID = "00000000-0000-0000-0000-000000000923"

CONFIG = {
    "version": 1,
    "catalog": {"default_plan": "pro"},
    "pricing": {
        "operations": {
            "completion": {
                "measures": {
                    "input_tokens": {"unit": "token"},
                    "output_tokens": {"unit": "token"},
                },
                "dimensions": {"model": {"type": "string"}},
            }
        },
        "rate_cards": {
            "standard": {
                "operations": {
                    "completion": {
                        "rules": [
                            {
                                "when": {
                                    "model": {
                                        "op": "prefix",
                                        "value": "premium-",
                                    }
                                },
                                "charge": {
                                    "type": "expression",
                                    "formula": "input_tokens * 2 + output_tokens * 3",
                                },
                            }
                        ],
                        "unmatched": {
                            "action": "charge",
                            "charge": {
                                "type": "expression",
                                "formula": "input_tokens + output_tokens",
                            },
                        },
                    }
                }
            }
        },
    },
    "credits": {
        "buckets": {
            "grant": {
                "priority": 10,
                "expiry": {
                    "type": "after_grant",
                    "interval": {"unit": "day", "count": 7},
                    "timezone": "UTC",
                },
            },
            "purchased": {"priority": 20, "expiry": {"type": "never"}},
        },
        "default_bucket": "purchased",
    },
    "plans": {"pro": {"display_name": "Pro", "rank": 0, "rate_card": "standard"}},
}

CONCURRENCY_CONFIG = {
    "version": 1,
    "catalog": {"default_plan": "max_two"},
    "pricing": deepcopy(CONFIG["pricing"]),
    "credits": deepcopy(CONFIG["credits"]),
    "admission": {
        "policies": {
            "max_two": {"max_in_flight": 2},
            "headroom": {"max_in_flight": 10},
        }
    },
    "plans": {
        "max_two": {
            "display_name": "Max two",
            "rank": 0,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
            "admission_policy": "max_two",
        },
        "headroom": {
            "display_name": "Headroom",
            "rank": 1,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
            "admission_policy": "headroom",
        },
    },
}


@pytest.fixture
def store(pg_database_url: str) -> Iterator[PostgresStore]:
    owned_store = PostgresStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        provider_environment="test",
    )
    try:
        yield owned_store
    finally:
        owned_store.close()


def _run_with_concurrent_stores[T](
    pg_database_url: str,
    worker_count: int,
    operation: Callable[[PostgresStore, int], T],
) -> list[T]:
    """Start one independently pooled store per worker at the same barrier."""
    start = Barrier(worker_count)

    def invoke(worker_index: int) -> T:
        with PostgresStore(
            pg_database_url,
            tenant_id=TEST_TENANT_ID,
            provider_environment="test",
            max_pool_size=1,
        ) as worker_store:
            start.wait(timeout=10)
            return operation(worker_store, worker_index)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(invoke, worker_index) for worker_index in range(worker_count)]
        return [future.result() for future in futures]


def _financial_snapshot(
    pg_database_url: str,
    user_id: str,
) -> tuple[Decimal, Decimal, int, int, int]:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                account.balance,
                COALESCE((
                    SELECT sum(entry.amount)
                    FROM bursar.credit_ledger_entries AS entry
                    WHERE entry.account_id = account.id
                ), 0),
                (
                    SELECT count(*)
                    FROM bursar.credit_ledger_entries AS entry
                    WHERE entry.account_id = account.id
                      AND entry.kind = 'usage'
                ),
                (
                    SELECT count(*)
                    FROM bursar.credit_usage_charges AS charge
                    WHERE charge.account_id = account.id
                ),
                (
                    SELECT count(DISTINCT charge.idempotency_key)
                    FROM bursar.credit_usage_charges AS charge
                    WHERE charge.account_id = account.id
                )
            FROM bursar.credit_accounts AS account
            WHERE account.tenant_id = %s
              AND account.subject_id = %s
              AND account.account_kind = 'personal'
            """,
            [TEST_TENANT_ID, user_id],
        )
        row = cursor.fetchone()
    assert row is not None
    balance, ledger_total, usage_entries, usage_charges, usage_keys = row
    return balance, ledger_total, usage_entries, usage_charges, usage_keys


def _active_lease_snapshot(
    pg_database_url: str,
    user_id: str,
) -> tuple[Decimal, int, Decimal]:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                account.balance,
                count(lease.id) FILTER (
                    WHERE lease.status = 'active' AND lease.expires_at > now()
                ),
                COALESCE(sum(lease.reserved_amount) FILTER (
                    WHERE lease.status = 'active' AND lease.expires_at > now()
                ), 0)
            FROM bursar.credit_accounts AS account
            LEFT JOIN bursar.credit_leases AS lease
              ON lease.account_id = account.id
            WHERE account.tenant_id = %s
              AND account.subject_id = %s
              AND account.account_kind = 'personal'
            GROUP BY account.id, account.balance
            """,
            [TEST_TENANT_ID, user_id],
        )
        row = cursor.fetchone()
    assert row is not None
    balance, active_count, reserved_total = row
    return balance, active_count, reserved_total


def _team_creation_snapshot(
    pg_database_url: str,
    idempotency_key: str,
) -> dict[str, object]:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                team.id::text AS team_id,
                team.name,
                account.id::text AS account_id,
                account.balance,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_teams AS matching_team
                    WHERE matching_team.tenant_id = %s
                      AND matching_team.creation_idempotency_key = %s
                ) AS team_count,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_team_members AS member
                    WHERE member.team_id = team.id
                ) AS member_count,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_ledger_entries AS entry
                    WHERE entry.account_id = account.id
                      AND entry.kind = 'grant'
                      AND entry.operation = 'team_initial_grant'
                ) AS initial_grant_count,
                (
                    SELECT count(*)::int
                    FROM bursar.subjects AS subject
                    WHERE subject.tenant_id = %s
                ) AS tenant_subject_count,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_teams AS tenant_team
                    WHERE tenant_team.tenant_id = %s
                ) AS tenant_team_count,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_team_members AS tenant_member
                    WHERE tenant_member.tenant_id = %s
                ) AS tenant_member_count,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_accounts AS tenant_account
                    WHERE tenant_account.tenant_id = %s
                ) AS tenant_account_count,
                (
                    SELECT count(*)::int
                    FROM bursar.credit_ledger_entries AS tenant_entry
                    WHERE tenant_entry.tenant_id = %s
                ) AS tenant_ledger_count
            FROM bursar.credit_teams AS team
            JOIN bursar.credit_accounts AS account
              ON account.subject_id = team.subject_id
             AND account.account_kind = 'team'
            WHERE team.tenant_id = %s
              AND team.creation_idempotency_key = %s
            """,
            [
                TEST_TENANT_ID,
                idempotency_key,
                TEST_TENANT_ID,
                TEST_TENANT_ID,
                TEST_TENANT_ID,
                TEST_TENANT_ID,
                TEST_TENANT_ID,
                TEST_TENANT_ID,
                idempotency_key,
            ],
        )
        row = cursor.fetchone()
        assert row is not None
        assert cursor.description is not None
        return dict(zip((column.name for column in cursor.description), row, strict=True))


def test_catalog_shape_validator_rejects_removed_nested_fields(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            WITH schema AS (
                SELECT bursar.catalog_document_shape_schema() AS document
            )
            SELECT extensions.jsonb_matches_schema(
                schema.document::json,
                '{"version":1,"credits":{},"catalog":{"activation":{"mode":"on_publish"}}}'::jsonb
            )
            FROM schema
            """
        )
        assert cursor.fetchone()[0] is False  # type: ignore[reportOptionalSubscript]

        cursor.execute(
            """
            WITH schema AS (
                SELECT bursar.catalog_document_shape_schema() AS document
            )
            SELECT extensions.jsonb_matches_schema(
                jsonb_build_object(
                    '$defs', schema.document->'$defs',
                    '$ref', '#/$defs/AutoRechargeLimits'
                )::json,
                '{"max_purchases":1,"window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"max_charge_minor":100,"cooldown":{"unit":"hour","count":1},"max_failures":3}'::jsonb
            )
            FROM schema
            """
        )
        assert cursor.fetchone()[0] is False  # type: ignore[reportOptionalSubscript]


def test_migrations_are_idempotent_and_detect_checksum_mismatch(
    pg_database_url: str,
) -> None:
    run_migrations(pg_database_url)
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version, checksum FROM bursar.schema_migrations ORDER BY version LIMIT 1")
        version, checksum = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        cursor.execute(
            "UPDATE bursar.schema_migrations SET checksum = 'tampered' WHERE version = %s",
            [version],
        )

    try:
        with pytest.raises(StoreError, match="checksum mismatch"):
            run_migrations(pg_database_url)
    finally:
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE bursar.schema_migrations SET checksum = %s WHERE version = %s",
                [checksum, version],
            )

    run_migrations(pg_database_url)

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO bursar.schema_migrations(version, checksum) VALUES (%s, %s)",
            ["999_obsolete.sql", "obsolete"],
        )
    try:
        with pytest.raises(StoreError, match="absent from this release"):
            run_migrations(pg_database_url)
    finally:
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM bursar.schema_migrations WHERE version = %s",
                ["999_obsolete.sql"],
            )


@pytest.mark.concurrency
def test_migration_lock_timeout_fails_promptly_and_recovers(pg_database_url: str) -> None:
    holder = psycopg2.connect(pg_database_url)
    try:
        with holder.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["bursar:migrations"],
            )

        started_at = time.monotonic()
        with pytest.raises(StoreError, match="lock timeout"):
            run_migrations(pg_database_url, lock_timeout_ms=100)
        assert time.monotonic() - started_at < 5
    finally:
        holder.rollback()
        holder.close()

    run_migrations(pg_database_url, lock_timeout_ms=1_000)


@pytest.mark.concurrency
def test_concurrent_migrations_serialize_pristine_database(pg_database_url: str) -> None:
    database_name = f"bursar_migration_race_{uuid4().hex}"
    admin_dsn = make_dsn(pg_database_url, dbname="postgres")
    pristine_dsn = make_dsn(pg_database_url, dbname=database_name)

    admin = psycopg2.connect(admin_dsn)
    try:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    finally:
        admin.close()

    try:
        start = Barrier(2)

        def migrate() -> None:
            start.wait()
            run_migrations(pristine_dsn)

        with ThreadPoolExecutor(max_workers=2) as executor:
            migrations = [executor.submit(migrate) for _ in range(2)]
            for migration in migrations:
                migration.result()

        with psycopg2.connect(pristine_dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM bursar.schema_migrations")
            assert cursor.fetchone()[0] > 0  # type: ignore[reportOptionalSubscript]
    finally:
        admin = psycopg2.connect(admin_dsn)
        try:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    [database_name],
                )
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
        finally:
            admin.close()


@pytest.mark.concurrency
def test_concurrent_unique_deductions_prevent_double_spend(
    pg_database_url: str,
    store: PostgresStore,
) -> None:
    user_id = "00000000-0000-0000-0000-000000000921"
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(deepcopy(CONCURRENCY_CONFIG))
    service.add_credits(
        user_id,
        Decimal("5"),
        entry_type="purchase",
        idempotency_key="concurrent-double-spend-funding",
    )

    def charge(worker_store: PostgresStore, worker_index: int) -> DeductionResult:
        return worker_store.deduct_with_allowance(
            user_id,
            Decimal("1"),
            operation="completion",
            idempotency_key=f"concurrent-double-spend:{worker_index}",
        )

    results = _run_with_concurrent_stores(pg_database_url, 12, charge)
    successes = [result for result in results if result.error is None]
    failures = [result for result in results if result.error is not None]

    assert len(successes) == 5
    assert len(failures) == 7
    assert {result.error for result in failures} == {"insufficient_credits"}
    assert len({result.entry_id for result in successes}) == 5
    assert {result.balance_after for result in successes} == {
        Decimal("0"),
        Decimal("1"),
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
    }

    balance, ledger_total, usage_entries, usage_charges, usage_keys = _financial_snapshot(
        pg_database_url,
        user_id,
    )
    assert balance == Decimal("0")
    assert ledger_total == balance
    assert usage_entries == 5
    assert usage_charges == 5
    assert usage_keys == 5


@pytest.mark.concurrency
def test_concurrent_same_key_deduction_replays_one_logical_debit(
    pg_database_url: str,
    store: PostgresStore,
) -> None:
    user_id = "00000000-0000-0000-0000-000000000922"
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(deepcopy(CONCURRENCY_CONFIG))
    service.add_credits(
        user_id,
        Decimal("10"),
        entry_type="purchase",
        idempotency_key="concurrent-replay-funding",
    )

    def replay(worker_store: PostgresStore, _worker_index: int) -> DeductionResult:
        return worker_store.deduct_with_allowance(
            user_id,
            Decimal("2"),
            operation="completion",
            idempotency_key="concurrent-replay-one-debit",
        )

    results = _run_with_concurrent_stores(pg_database_url, 12, replay)

    assert all(result.error is None for result in results)
    assert len({result.entry_id for result in results}) == 1
    assert len({result.usage_charge_id for result in results}) == 1
    assert sum(not result.idempotent for result in results) == 1
    assert sum(result.idempotent for result in results) == 11
    assert {result.balance_after for result in results} == {Decimal("8")}

    balance, ledger_total, usage_entries, usage_charges, usage_keys = _financial_snapshot(
        pg_database_url,
        user_id,
    )
    assert balance == Decimal("8")
    assert ledger_total == balance
    assert usage_entries == 1
    assert usage_charges == 1
    assert usage_keys == 1


@pytest.mark.concurrency
def test_concurrent_lease_admission_enforces_max_concurrent_and_headroom(
    pg_database_url: str,
    store: PostgresStore,
) -> None:
    max_concurrent_user = "00000000-0000-0000-0000-000000000923"
    headroom_user = "00000000-0000-0000-0000-000000000924"
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(deepcopy(CONCURRENCY_CONFIG))
    service.add_credits(
        max_concurrent_user,
        Decimal("100"),
        entry_type="purchase",
        idempotency_key="concurrent-lease-limit-funding",
    )
    service.add_credits(
        headroom_user,
        Decimal("5"),
        entry_type="purchase",
        idempotency_key="concurrent-lease-headroom-funding",
    )
    service.set_user_plan(headroom_user, "headroom")

    def acquire_with_limit(worker_store: PostgresStore, worker_index: int) -> LeaseResult:
        return worker_store.create_lease(
            max_concurrent_user,
            Decimal("1"),
            "completion",
            CreateLeaseOptions(
                idempotency_key=f"concurrent-lease-limit:{worker_index}",
                floor=Decimal("0"),
                max_concurrent=2,
                ttl_seconds=60,
            ),
        )

    limited_results = _run_with_concurrent_stores(pg_database_url, 12, acquire_with_limit)
    limited_successes = [result for result in limited_results if result.error is None]
    limited_failures = [result for result in limited_results if result.error is not None]
    assert len(limited_successes) == 2
    assert len({result.lease_id for result in limited_successes}) == 2
    assert len(limited_failures) == 10
    assert {result.error for result in limited_failures} == {"max_concurrent_reached"}

    def acquire_against_headroom(worker_store: PostgresStore, worker_index: int) -> LeaseResult:
        return worker_store.create_lease(
            headroom_user,
            Decimal("2"),
            "completion",
            CreateLeaseOptions(
                idempotency_key=f"concurrent-lease-headroom:{worker_index}",
                floor=Decimal("0"),
                max_concurrent=10,
                ttl_seconds=60,
            ),
        )

    headroom_results = _run_with_concurrent_stores(pg_database_url, 12, acquire_against_headroom)
    headroom_successes = [result for result in headroom_results if result.error is None]
    headroom_failures = [result for result in headroom_results if result.error is not None]
    assert len(headroom_successes) == 2
    assert len({result.lease_id for result in headroom_successes}) == 2
    assert len(headroom_failures) == 10
    assert {result.error for result in headroom_failures} == {"insufficient_headroom"}

    assert _active_lease_snapshot(pg_database_url, max_concurrent_user) == (
        Decimal("100"),
        2,
        Decimal("2"),
    )
    assert _active_lease_snapshot(pg_database_url, headroom_user) == (
        Decimal("5"),
        2,
        Decimal("4"),
    )
    limited_availability = store.get_available(max_concurrent_user)
    headroom_availability = store.get_available(headroom_user)
    assert (limited_availability.available, limited_availability.reserved) == (
        Decimal("98"),
        Decimal("2"),
    )
    assert (headroom_availability.available, headroom_availability.reserved) == (
        Decimal("1"),
        Decimal("4"),
    )


def test_add_credits_idempotent_replay_uses_one_ledger_entry(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(CONFIG)
    first = service.add_credits(
        REPLAY_USER_ID,
        Decimal("25"),
        entry_type="purchase",
        idempotency_key="integration:add-replay",
    )
    replay = service.add_credits(
        REPLAY_USER_ID,
        Decimal("25"),
        entry_type="purchase",
        idempotency_key="integration:add-replay",
    )

    assert replay.entry_id == first.entry_id
    assert service.get_balance(REPLAY_USER_ID).balance == Decimal("25")


def test_public_config_round_trips_and_prices_generic_usage(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    config = deepcopy(CONFIG)
    config["pricing"]["operations"]["free_export"] = {
        "measures": {"calls": {"unit": "call"}},
        "dimensions": {},
    }
    config["pricing"]["rate_cards"]["standard"]["operations"]["free_export"] = {
        "rules": [],
        "unmatched": {"action": "charge", "charge": {"type": "flat", "amount": "0"}},
    }
    service.publish_and_activate_catalog(config)
    service.add_credits(
        USER_ID,
        Decimal("100"),
        entry_type="purchase",
        bucket="purchased",
        idempotency_key="new-schema-grant-1",
    )

    deduction = service.deduct(
        USER_ID,
        UsageMetrics(
            operation="completion",
            measures={"input_tokens": Decimal("2"), "output_tokens": Decimal("4")},
            dimensions={"model": "premium-x"},
        ),
        idempotency_key="new-schema-charge-1",
    )

    assert deduction.amount == Decimal("16")
    free = service.deduct(
        USER_ID,
        UsageMetrics(operation="free_export", measures={"calls": Decimal("1")}, dimensions={}),
        idempotency_key="new-schema-free-usage",
    )
    assert free.amount == Decimal("0")
    usage = service.list_usage_charges(USER_ID, limit=200)
    free_charge = next(item for item in usage.items if item.idempotency_key == "new-schema-free-usage")
    assert free_charge.operation == "free_export"
    assert free_charge.charged == Decimal("0")
    assert service.get_balance(USER_ID).balance == Decimal("84")
    loaded = store.get_active_catalog()
    assert loaded is not None
    assert loaded.config["pricing"]["operations"]["completion"]


def test_record_usage_appends_external_usage_without_debiting(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(CONFIG)
    service.add_credits(
        USER_ID,
        Decimal("100"),
        entry_type="purchase",
        bucket="purchased",
        idempotency_key="record-only-grant",
    )
    balance_before = service.get_balance(USER_ID).balance
    metrics = UsageMetrics(
        operation="completion",
        measures={"input_tokens": Decimal("2"), "output_tokens": Decimal("4")},
        dimensions={"model": "premium-x"},
    )
    metadata = CreditMetadata.model_validate(
        {
            "reference_type": "provider_request",
            "reference_id": "request-1",
            "provider_request_id": "request-1",
        }
    )

    first = service.record_usage(
        USER_ID,
        metrics,
        idempotency_key="roadmap-1:usage:outline",
        metadata=metadata,
    )
    replay = service.record_usage(
        USER_ID,
        metrics,
        idempotency_key="roadmap-1:usage:outline",
        metadata=metadata,
    )

    assert first.requested == Decimal("16")
    assert first.idempotent is False
    assert replay.usage_id == first.usage_id
    assert replay.idempotent is True
    assert service.get_balance(USER_ID).balance == balance_before

    rows = service.list_usage_charges(USER_ID, limit=200).items
    recorded = [row for row in rows if row.idempotency_key == "roadmap-1:usage:outline"]
    assert len(recorded) == 1
    assert recorded[0].billing_disposition == "record_only"
    assert recorded[0].requested == Decimal("16")
    assert recorded[0].charged == Decimal("0")
    assert recorded[0].allowance_requested == Decimal("0")
    assert recorded[0].allowance_covered == Decimal("0")
    assert recorded[0].metadata is not None
    assert recorded[0].metadata["provider_request_id"] == "request-1"
    now = datetime.now(UTC)
    assert service.spend_by_user(now - timedelta(minutes=5), now + timedelta(minutes=5)) == []

    with pytest.raises(StoreError, match="idempotency_conflict"):
        service.record_usage(
            USER_ID,
            UsageMetrics(
                operation="completion",
                measures={"input_tokens": Decimal("3"), "output_tokens": Decimal("4")},
                dimensions={"model": "premium-x"},
            ),
            idempotency_key="roadmap-1:usage:outline",
            metadata=metadata,
        )

    # Record-only telemetry follows bounded usage retention, while an equally
    # old billable receipt remains permanent accounting evidence.
    service.deduct(
        USER_ID,
        metrics,
        idempotency_key="roadmap-1:billable-retention-control",
    )
    maintenance_now = datetime.now(UTC)
    old_event_at = maintenance_now - timedelta(days=100)
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.mutation_context', 'internal', true)")
        # The payload's partition key is deliberately tied to the permanent
        # usage fact. Move the child rows out while this retention fixture
        # backdates both sides of that composite relationship.
        cursor.execute(
            """
            CREATE TEMP TABLE moved_usage_payloads ON COMMIT DROP AS
            SELECT payload.*
            FROM bursar.usage_charge_payloads AS payload
            JOIN bursar.credit_usage_charges AS charge
              ON charge.id = payload.charge_id
             AND charge.event_at = payload.event_at
            WHERE charge.idempotency_key IN (%s, %s)
            """,
            [
                "roadmap-1:usage:outline",
                "roadmap-1:billable-retention-control",
            ],
        )
        cursor.execute(
            """
            DELETE FROM bursar.usage_charge_payloads AS payload
            USING bursar.credit_usage_charges AS charge
            WHERE charge.id = payload.charge_id
              AND charge.event_at = payload.event_at
              AND charge.idempotency_key IN (%s, %s)
            """,
            [
                "roadmap-1:usage:outline",
                "roadmap-1:billable-retention-control",
            ],
        )
        cursor.execute(
            """
            UPDATE bursar.credit_usage_charges
            SET event_at = %s
            WHERE idempotency_key IN (%s, %s)
            """,
            [
                old_event_at,
                "roadmap-1:usage:outline",
                "roadmap-1:billable-retention-control",
            ],
        )
        assert cursor.rowcount == 2
        cursor.execute(
            """
            INSERT INTO bursar.usage_charge_payloads (
                tenant_id,
                charge_id,
                event_at,
                measures,
                feature,
                model,
                region,
                dimensions,
                metadata,
                pricing_snapshot,
                created_at
            )
            SELECT
                tenant_id,
                charge_id,
                %s,
                measures,
                feature,
                model,
                region,
                dimensions,
                metadata,
                pricing_snapshot,
                created_at
            FROM moved_usage_payloads
            """,
            [old_event_at],
        )
        assert cursor.rowcount == 2
        cursor.execute(
            "SELECT bursar.run_storage_maintenance(%s)",
            [maintenance_now],
        )
        maintenance = cursor.fetchone()[0]  # type: ignore[index]
        assert maintenance["record_only_usage_purged"] == 1
        cursor.execute(
            """
            SELECT idempotency_key, billing_disposition
            FROM bursar.credit_usage_charges
            WHERE idempotency_key IN (%s, %s)
            ORDER BY idempotency_key
            """,
            [
                "roadmap-1:usage:outline",
                "roadmap-1:billable-retention-control",
            ],
        )
        assert cursor.fetchall() == [("roadmap-1:billable-retention-control", "billable")]


def test_lease_settlement_and_refund_follow_revamped_rpc_contracts(store: PostgresStore) -> None:
    config = deepcopy(CONFIG)
    config["entitlements"] = {
        "features": {
            "tutor_chat": {
                "type": "boolean",
                "default": False,
            }
        }
    }
    config["plans"]["pro"].update(
        {
            "allowed_operations": ["completion"],
            "features": {"tutor_chat": True},
            "quotas": {
                "completion_input": {
                    "operation": "completion",
                    "measure": "input_tokens",
                    "limit": "5",
                    "window": {
                        "type": "calendar",
                        "unit": "month",
                        "count": 1,
                        "timezone": "UTC",
                    },
                    "enforcement": "block",
                }
            },
        }
    )
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(config)
    service.set_user_plan(USER_ID, "pro")
    service.add_credits(
        USER_ID,
        Decimal("100"),
        entry_type="purchase",
        bucket="purchased",
        idempotency_key="lease-contract-grant",
    )
    estimate = UsageMetrics(
        operation="completion",
        measures={"input_tokens": Decimal("5"), "output_tokens": Decimal("2")},
        dimensions={"model": "standard"},
    )
    actual = UsageMetrics(
        operation="completion",
        measures={"input_tokens": Decimal("3"), "output_tokens": Decimal("1")},
        dimensions={"model": "standard"},
    )

    lease = service.reserve(
        USER_ID,
        estimate,
        ReserveOptions(
            operation_type="completion",
            feature="tutor_chat",
            idempotency_key="lease-contract-reserve",
        ),
    )
    assert lease.lease_id is not None
    renewed = service.renew(USER_ID, lease.lease_id, ttl=300)

    # A plan/catalog change after admission must not reprice work already in
    # flight. The lease's immutable revision and rate card own settlement.
    changed_config = deepcopy(config)
    changed_config["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["unmatched"]["charge"][
        "formula"
    ] = "input_tokens * 100 + output_tokens * 100"
    service.publish_and_activate_catalog(changed_config)
    service.set_user_plan(USER_ID, "pro")

    deduction = service.settle(
        USER_ID,
        lease.lease_id,
        actual,
        SettleOptions(
            feature="tutor_chat",
            idempotency_key="lease-contract-settle",
            metadata=CreditMetadata.model_validate(
                {
                    "audit_context": {
                        "future_provider_extension": "y" * 32768,
                    }
                }
            ),
        ),
    )
    assert deduction.entry_id is not None
    refund = service.refund_credits(
        deduction.entry_id,
        reason="integration_test",
        idempotency_key="lease-contract-refund",
    )
    replay = service.refund_credits(
        deduction.entry_id,
        reason="integration_test",
        idempotency_key="lease-contract-refund",
    )

    assert lease.expires_at is not None
    assert renewed.expires_at is not None
    assert deduction.amount == Decimal("4")
    assert deduction.balance_after == Decimal("96")
    assert refund.amount == Decimal("4")
    assert refund.new_balance == Decimal("100")
    assert replay.refund_entry_id == refund.refund_entry_id
    assert service.get_balance(USER_ID).balance == Decimal("100")

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                length(
                    payload.metadata
                    -> 'audit_context'
                    ->> 'future_provider_extension'
                ),
                ledger.metadata ? 'audit_context',
                quota.metadata ? 'audit_context'
            FROM bursar.usage_charge_payloads AS payload
            JOIN bursar.credit_usage_charges AS charge
              ON charge.id = payload.charge_id
             AND charge.event_at = payload.event_at
            JOIN bursar.credit_ledger_entries AS ledger
              ON ledger.id = charge.ledger_entry_id
            LEFT JOIN bursar.quota_usage_events AS quota
              ON quota.usage_charge_id = charge.id
            WHERE charge.ledger_entry_id = %s
            """,
            [deduction.entry_id],
        )
        provider_extension_length, ledger_has_raw, quota_has_raw = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        cursor.execute(
            """
            SELECT
                COALESCE(sum(event.amount), 0),
                COALESCE(max(quota_window.consumed), 0)
            FROM bursar.credit_accounts AS account
            LEFT JOIN bursar.quota_usage_events AS event
              ON event.account_id = account.id
             AND event.quota_key = 'completion_input'
            LEFT JOIN bursar.quota_windows AS quota_window
              ON quota_window.account_id = account.id
             AND quota_window.quota_key = 'completion_input'
            WHERE account.subject_id = %s
            """,
            [USER_ID],
        )
        usage_total, cached_consumed = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]

    assert usage_total == Decimal("0")
    assert cached_consumed == Decimal("0")
    assert provider_extension_length == 32768
    assert ledger_has_raw is False
    assert quota_has_raw is False


def test_bucket_priority_is_applied_by_postgres_store(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(CONFIG)
    service.add_credits(
        USER_ID,
        Decimal("10"),
        entry_type="purchase",
        bucket="grant",
        idempotency_key="spend-order-grant",
    )
    service.add_credits(
        USER_ID,
        Decimal("10"),
        entry_type="purchase",
        bucket="purchased",
        idempotency_key="spend-order-purchased",
    )

    service.deduct(
        USER_ID,
        UsageMetrics(
            operation="completion",
            measures={"input_tokens": Decimal("5"), "output_tokens": Decimal("0")},
            dimensions={"model": "standard"},
        ),
        idempotency_key="new-schema-charge-2",
    )
    buckets = {row.bucket_key: row.balance for row in service.get_bucket_balances(USER_ID).buckets}
    assert buckets["grant"] == Decimal("5")
    assert buckets["purchased"] == Decimal("10")


def test_account_created_grant_program_posts_every_award(
    store: PostgresStore,
) -> None:
    config = deepcopy(CONFIG)
    config["credits"]["grant_programs"] = {
        "welcome": {
            "trigger": "account_created",
            "awards": [
                {
                    "recipient": "subject",
                    "amount": "2",
                    "bucket": "purchased",
                },
                {
                    "recipient": "subject",
                    "amount": "3",
                    "bucket": "purchased",
                },
            ],
            "max_awards_per_subject": 1,
            "idempotency_scope": "subject",
        }
    }
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(config)

    service.add_credits(
        REPLAY_USER_ID,
        Decimal("1"),
        entry_type="purchase",
        idempotency_key="grant-program-trigger",
    )

    assert service.get_balance(REPLAY_USER_ID).balance == Decimal("6")
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.grant_award_executions AS execution
            JOIN bursar.grant_program_events AS event
              ON event.id = execution.grant_event_id
            WHERE event.subject_id = %s
            """,
            [REPLAY_USER_ID],
        )
        assert cursor.fetchone() == (2,)


def test_manual_grant_program_is_exposed_by_python_sdk(store: PostgresStore) -> None:
    config = deepcopy(CONFIG)
    config["credits"]["grant_programs"] = {
        "manual_bonus": {
            "trigger": "manual",
            "awards": [
                {
                    "recipient": "subject",
                    "amount": "4",
                    "bucket": "purchased",
                }
            ],
            "max_awards_per_subject": 1,
            "idempotency_scope": "event",
        }
    }
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(config)
    request = ExecuteGrantProgramRequest(
        trigger="manual",
        program_key="manual_bonus",
        subject_id=USER_ID,
        event_key="manual-event-1",
        metadata=CreditMetadata.model_validate({"campaign": "summer"}),
    )

    awards = service.execute_grant_program(request)
    replay = service.execute_grant_program(request)

    assert len(awards) == 1
    assert awards[0].amount == Decimal("4")
    assert awards[0].recipient_subject_id == USER_ID
    assert awards[0].replayed is False
    assert len(replay) == 1
    assert replay[0].replayed is True
    assert service.get_balance(USER_ID).balance == Decimal("4")


def test_expire_leases_is_exposed_by_python_store(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(CONFIG)
    service.add_credits(USER_ID, Decimal("10"), idempotency_key="lease-expiry-credit")
    lease = store.create_lease(
        USER_ID,
        Decimal("2"),
        "completion",
        CreateLeaseOptions(
            idempotency_key="lease-expiry-reserve",
            ttl_seconds=1,
        ),
    )
    time.sleep(1.1)

    assert store.expire_leases(25) == 1
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status FROM bursar.credit_leases WHERE id = %s", [lease.lease_id])
        assert cursor.fetchone() == ("expired",)


def test_remove_team_member_is_exposed_by_python_store(store: PostgresStore) -> None:
    team = store.create_team(USER_ID, "SDK team", idempotency_key="team:create:sdk")
    store.add_team_member(team.team_id, REPLAY_USER_ID)

    assert store.remove_team_member(team.team_id, REPLAY_USER_ID) is True
    assert store.remove_team_member(team.team_id, REPLAY_USER_ID) is False
    assert store.remove_team_member(team.team_id, USER_ID) is False


def test_team_creation_replay_posts_one_initial_grant(store: PostgresStore) -> None:
    CreditsService(store=store).publish_and_activate_catalog(CONFIG)
    idempotency_key = "team:create:replay"

    first = store.create_team(
        TEAM_REPLAY_OWNER_ID,
        "Replay-safe team",
        Decimal("9.000"),
        idempotency_key=idempotency_key,
    )
    before_replay = _team_creation_snapshot(store.database_url, idempotency_key)
    replay = store.create_team(
        TEAM_REPLAY_OWNER_ID,
        "Replay-safe team",
        Decimal("9"),
        idempotency_key=idempotency_key,
    )

    assert first.idempotent is False
    assert replay.model_copy(update={"idempotent": False}) == first
    assert replay.idempotent is True
    after_replay = _team_creation_snapshot(store.database_url, idempotency_key)
    assert after_replay == before_replay
    assert after_replay["team_id"] == first.team_id
    assert after_replay["name"] == "Replay-safe team"
    assert after_replay["balance"] == Decimal("9.000000")
    assert after_replay["team_count"] == 1
    assert after_replay["member_count"] == 1
    assert after_replay["initial_grant_count"] == 1


def test_team_creation_conflicts_have_no_persistent_side_effects(store: PostgresStore) -> None:
    CreditsService(store=store).publish_and_activate_catalog(CONFIG)
    idempotency_key = "team:create:conflict"
    store.create_team(
        TEAM_REPLAY_OWNER_ID,
        "Conflict-safe team",
        Decimal("7"),
        idempotency_key=idempotency_key,
    )
    before = _team_creation_snapshot(store.database_url, idempotency_key)

    with pytest.raises(StoreError, match="^idempotency_conflict$"):
        store.create_team(
            TEAM_REPLAY_OWNER_ID,
            "Changed team",
            Decimal("7"),
            idempotency_key=idempotency_key,
        )
    with pytest.raises(StoreError, match="^idempotency_conflict$"):
        store.create_team(
            TEAM_REPLAY_OWNER_ID,
            "Conflict-safe team",
            Decimal("8"),
            idempotency_key=idempotency_key,
        )
    with pytest.raises(StoreError, match="^idempotency_conflict$"):
        store.create_team(
            TEAM_CHANGED_OWNER_ID,
            "Conflict-safe team",
            Decimal("7"),
            idempotency_key=idempotency_key,
        )

    assert _team_creation_snapshot(store.database_url, idempotency_key) == before


@pytest.mark.concurrency
def test_concurrent_team_creation_returns_one_logical_team(store: PostgresStore) -> None:
    CreditsService(store=store).publish_and_activate_catalog(CONFIG)
    idempotency_key = "team:create:concurrent"
    worker_count = 12

    results = _run_with_concurrent_stores(
        store.database_url,
        worker_count,
        lambda worker_store, _worker_index: worker_store.create_team(
            TEAM_CONCURRENT_OWNER_ID,
            "Concurrent team",
            Decimal("5"),
            idempotency_key=idempotency_key,
        ),
    )

    assert len({result.team_id for result in results}) == 1
    assert sum(not result.idempotent for result in results) == 1
    assert sum(result.idempotent for result in results) == worker_count - 1
    snapshot = _team_creation_snapshot(store.database_url, idempotency_key)
    assert snapshot["team_id"] == results[0].team_id
    assert snapshot["name"] == "Concurrent team"
    assert snapshot["balance"] == Decimal("5.000000")
    assert snapshot["team_count"] == 1
    assert snapshot["member_count"] == 1
    assert snapshot["initial_grant_count"] == 1


def test_plan_policies_persist_as_typed_references(
    store: PostgresStore,
) -> None:
    config = deepcopy(CONFIG)
    config["pricing"]["operations"]["completion"]["measures"]["calls"] = {"unit": "call"}
    config["credits"]["policies"] = {"line": {"type": "credit_line", "limit": "20"}}
    config["admission"] = {
        "policies": {
            "pro": {
                "max_in_flight": 2,
                "operations": {"completion": {"max_in_flight": 1}},
            }
        }
    }
    config["plans"]["pro"].update(
        {
            "credit_policy": "line",
            "admission_policy": "pro",
            "quotas": {
                "completion_calls": {
                    "operation": "completion",
                    "measure": "calls",
                    "limit": "5",
                    "window": {
                        "type": "rolling",
                        "duration": {"unit": "hour", "count": 1},
                    },
                    "enforcement": "block",
                }
            },
        }
    )

    CreditsService(store=store).publish_and_activate_catalog(config)

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT credit_policy_key, admission_policy_key
            FROM bursar.catalog_plans
            WHERE plan_key = 'pro'
              AND catalog_revision_id = (
                SELECT id
                FROM bursar.catalog_revisions
                WHERE status = 'active'
              )
            """
        )
        assert cursor.fetchone() == ("line", "pro")

        cursor.execute(
            """
            SELECT operation_key, measure_key, quota_limit, window_policy, enforcement
            FROM bursar.catalog_plan_quotas
            WHERE plan_key = 'pro'
              AND quota_key = 'completion_calls'
              AND catalog_revision_id = (
                SELECT id
                FROM bursar.catalog_revisions
                WHERE status = 'active'
              )
            """
        )
        operation, measure, quota_limit, window, enforcement = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]

    assert operation == "completion"
    assert measure == "calls"
    assert quota_limit == Decimal("5")
    assert window == {
        "type": "rolling",
        "duration": {"unit": "hour", "count": 1},
    }
    assert enforcement == "block"
