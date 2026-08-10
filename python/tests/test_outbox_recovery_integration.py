"""Real-PostgreSQL tests for tenant-scoped outbox recovery RPCs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg2
import pytest

from bursar.errors import StoreError
from bursar.shared.postgres_client import PostgresClient
from bursar.storage import OutboxDeadLetterListOptions
from bursar.storage.postgres_repository import PostgresStorageRepository
from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]

OTHER_TENANT_ID = "00000000-0000-0000-0000-000000000002"


def _seed_dead_letters(pg_database_url: str) -> list[str]:
    event_ids: list[str] = []
    created_at = datetime(2026, 8, 10, tzinfo=UTC)
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT bursar.create_tenant(%s::uuid, %s::text, %s::text)",
            (OTHER_TENANT_ID, "outbox-other", "Outbox other tenant"),
        )
        for index in range(2):
            cursor.execute(
                """
                INSERT INTO bursar.event_outbox(
                    tenant_id,
                    topic,
                    aggregate_type,
                    aggregate_id,
                    idempotency_key,
                    status,
                    attempt_count,
                    last_error,
                    created_at
                )
                VALUES (
                    %s::uuid,
                    'usage.charge_recorded',
                    'credit_usage_charge',
                    %s::uuid,
                    %s::text,
                    'dead_letter',
                    10,
                    'outbox_delivery_failed:RuntimeError',
                    %s::timestamptz
                )
                RETURNING id
                """,
                (
                    TEST_TENANT_ID,
                    str(uuid4()),
                    f"outbox-recovery-{index}",
                    created_at + timedelta(seconds=index),
                ),
            )
            row = cursor.fetchone()
            assert row is not None
            event_ids.append(str(row[0]))

        cursor.execute(
            """
            INSERT INTO bursar.event_outbox(
                tenant_id,
                topic,
                aggregate_type,
                aggregate_id,
                idempotency_key,
                status,
                attempt_count,
                last_error
            )
            VALUES (
                %s::uuid,
                'usage.charge_recorded',
                'credit_usage_charge',
                %s::uuid,
                'outbox-other-tenant',
                'dead_letter',
                10,
                'outbox_delivery_failed:RuntimeError'
            )
            """,
            (OTHER_TENANT_ID, str(uuid4())),
        )
    return event_ids


def test_recovery_rpc_cursor_requeue_renewal_and_tenant_boundary(pg_database_url: str) -> None:
    event_ids = _seed_dead_letters(pg_database_url)
    client = PostgresClient(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        access_role="bursar_operator",
        max_connections=2,
    )
    repository = PostgresStorageRepository(client.query, TEST_TENANT_ID)
    try:
        stats = repository.stats()
        assert stats.pending_count == 0
        assert stats.dead_letter_count == 2

        first_page = repository.list_dead_letters(OutboxDeadLetterListOptions(limit=1))
        assert [item.event_id for item in first_page.items] == [event_ids[0]]
        assert first_page.next_cursor is not None
        second_page = repository.list_dead_letters(OutboxDeadLetterListOptions(limit=1, cursor=first_page.next_cursor))
        assert [item.event_id for item in second_page.items] == [event_ids[1]]
        assert second_page.next_cursor is None

        assert repository.requeue(event_ids[0]) is True
        assert repository.requeue(event_ids[0]) is False
        after_requeue = repository.stats()
        assert after_requeue.pending_count == 1
        assert after_requeue.dead_letter_count == 1

        claimed = repository.claim(["usage.charge_recorded"], 1, 1)
        assert [item.event_id for item in claimed] == [event_ids[0]]
        assert repository.renew(claimed[0], 60) is True
        assert repository.complete(claimed[0]) is True
        assert repository.complete(claimed[0]) is False

        assert repository.requeue(event_ids[1]) is True
        retry = repository.claim(["usage.charge_recorded"], 1, 60)
        assert [item.event_id for item in retry] == [event_ids[1]]
        assert repository.fail(retry[0], "outbox_delivery_failed:RuntimeError", 0, 1) is True
        assert repository.stats().dead_letter_count == 1

        cross_tenant_repository = PostgresStorageRepository(client.query, OTHER_TENANT_ID)
        with pytest.raises(StoreError, match="PostgreSQL query failed") as error_info:
            cross_tenant_repository.stats()
        assert error_info.value.code == "STORE_ERROR"
        assert error_info.value.details is not None
        assert error_info.value.details["sql_state"] == "42501"
        assert error_info.value.retryable is False
        assert error_info.value.indeterminate is False
    finally:
        client.close()
