from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import psycopg2.extras
import pytest

import bursar.storage.cutover as cutover
from bursar.storage.cutover import _digest, _normalize
from bursar.storage.ports import UsageChargeExport

TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _usage_row():
    return {
        "tenant_id": TENANT_ID,
        "charge_id": "00000000-0000-0000-0000-000000000002",
        "account_id": "00000000-0000-0000-0000-000000000003",
        "subject_id": "00000000-0000-0000-0000-000000000004",
        "operation": "test",
        "feature": None,
        "model": None,
        "region": None,
        "measures": {"a": 1},
        "dimensions": {},
        "metadata": {"x": "y"},
        "requested": "1.000000",
        "charged": "0",
        "allowance_requested": "0",
        "allowance_covered": "0",
        "billing_disposition": "record_only",
        "catalog_revision_id": None,
        "plan_id": None,
        "rate_card_key": None,
        "pricing_snapshot": {},
        "ledger_entry_id": None,
        "correction_of_charge_id": None,
        "idempotency_key": "key",
        "request_digest": "ab",
        "event_at": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def test_normalization_compares_driver_types_without_losing_details():
    row = _usage_row()
    other = row | {
        "tenant_id": UUID(row["tenant_id"]),
        "measures": '{"a":1}',
        "metadata": '{"x":"y"}',
        "requested": Decimal("1"),
        "event_at": datetime(2026, 1, 1),  # noqa: DTZ001 -- ClickHouse returns naive UTC
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    assert _normalize(row) == _normalize(other)
    assert _digest(row) == _digest(other)
    assert _digest(row) != _digest(row | {"billing_disposition": "billable"})
    assert _digest(row) != _digest(row | {"correction_of_charge_id": row["charge_id"]})


class _CutoverCursor:
    def __init__(
        self,
        source: str,
        row: dict,
        *,
        lock_acquired: bool = True,
        payload_available: bool = True,
        fail_statement: str | None = None,
    ):
        self.source = source
        self.row = row
        self.lock_acquired = lock_acquired
        self.payload_available = payload_available
        self.fail_statement = fail_statement
        self.statements: list[str] = []
        self.single = None
        self.page_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement: str, _parameters=None):
        self.statements.append(statement)
        if self.fail_statement is not None and self.fail_statement in statement:
            raise RuntimeError("query failed")
        self.single = None
        if "pg_try_advisory_lock" in statement:
            self.single = (self.lock_acquired,)
        elif "begin_usage_backend_cutover" in statement:
            self.single = ({"cutoff": "2026-01-01T00:00:00+00:00", "backend": self.source},)
        elif "list_usage_cutover_postgres" in statement:
            self.page_calls += 1
        elif "import_usage_cutover_item" in statement:
            self.single = (self.row | {"payload_available": True},)

    def fetchone(self):
        return self.single

    def fetchall(self):
        if self.page_calls == 1:
            return [(self.row | {"payload_available": self.payload_available},)]
        return []


class _CutoverConnection:
    def __init__(self, cursor: _CutoverCursor):
        self._cursor = cursor
        self.autocommit = False

    def cursor(self, **_kwargs):
        return self._cursor


def _patch_cutover(monkeypatch, source: str, row: dict, *, lock_acquired: bool = True, payload_available: bool = True):
    cursor = _CutoverCursor(source, row, lock_acquired=lock_acquired, payload_available=payload_available)
    connection = _CutoverConnection(cursor)
    writes = []

    class Store:
        def __init__(self, options):
            self._client = options.client
            self._tenant_id = options.tenant_id
            self._quoted_table = "`bursar_usage_events`"
            self._INSERT_COLUMNS = ("tenant_id", "event_at", "charge_id")

        def check_schema_compatibility(self):
            return None

        def write_usage(self, usage, event_id):
            writes.append((usage, event_id))

        def _result_rows(self, result):
            return result.result_rows

    pages = [[row]]
    monkeypatch.setattr(cutover, "ClickHouseUsageStore", Store)
    monkeypatch.setattr(cutover, "_drain", lambda *_args: None)
    monkeypatch.setattr(cutover, "_clickhouse_has_earlier_rows", lambda *_args: False)
    monkeypatch.setattr(cutover, "_clickhouse_count", lambda *_args: 1)
    monkeypatch.setattr(cutover, "_clickhouse_item", lambda _store, _item: row)
    monkeypatch.setattr(cutover, "_clickhouse_page", lambda *_args: pages.pop(0) if pages else [])
    monkeypatch.setattr(psycopg2.extras, "register_default_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(psycopg2.extras, "register_default_jsonb", lambda *_args, **_kwargs: None)
    client = SimpleNamespace()
    return connection, cursor, writes, client


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_cutover_copies_verifies_and_switches_only_after_full_scan(monkeypatch, source):
    row = _usage_row()
    connection, cursor, writes, client = _patch_cutover(monkeypatch, source, row)

    count = cutover.run_cutover(
        connection,
        client,
        TENANT_ID,
        "clickhouse" if source == "postgres" else "postgres",
        datetime(2026, 1, 1, tzinfo=UTC),
        "bursar_usage_events",
    )

    assert count == 1
    assert connection.autocommit
    assert any("verify_usage_cutover_item" in statement for statement in cursor.statements)
    assert any("finish_usage_backend_cutover" in statement for statement in cursor.statements)
    assert any("pg_advisory_unlock" in statement for statement in cursor.statements)
    assert len(writes) == (1 if source == "postgres" else 0)
    assert any("import_usage_cutover_item" in statement for statement in cursor.statements) == (source == "clickhouse")


def test_cutover_releases_driver_lock_when_source_payload_is_missing(monkeypatch):
    connection, cursor, _writes, client = _patch_cutover(monkeypatch, "postgres", _usage_row(), payload_available=False)

    with pytest.raises(RuntimeError, match="retained PostgreSQL usage payload is missing"):
        cutover.run_cutover(
            connection,
            client,
            TENANT_ID,
            "clickhouse",
            datetime(2026, 1, 1, tzinfo=UTC),
            "bursar_usage_events",
        )

    assert any("pg_advisory_unlock" in statement for statement in cursor.statements)


def test_cutover_refuses_to_start_when_another_driver_owns_the_lock(monkeypatch):
    connection, cursor, _writes, client = _patch_cutover(monkeypatch, "postgres", _usage_row(), lock_acquired=False)

    with pytest.raises(RuntimeError, match="another cutover driver"):
        cutover.run_cutover(
            connection,
            client,
            TENANT_ID,
            "clickhouse",
            datetime(2026, 1, 1, tzinfo=UTC),
            "bursar_usage_events",
        )

    assert not any("begin_usage_backend_cutover" in statement for statement in cursor.statements)


def test_clickhouse_lookup_helpers_page_rows_and_reject_duplicate_identity():
    row = _usage_row()

    class Client:
        def __init__(self):
            self.results = [
                SimpleNamespace(result_rows=[(1,)]),
                SimpleNamespace(result_rows=[(2,)]),
                SimpleNamespace(result_rows=[row]),
                SimpleNamespace(result_rows=[row]),
                SimpleNamespace(result_rows=[row, row]),
            ]
            self.queries = []

        def query(self, query, *, parameters):
            self.queries.append((query, parameters))
            return self.results.pop(0)

    class Store:
        def __init__(self):
            self._client = Client()
            self._tenant_id = UUID(TENANT_ID)
            self._quoted_table = "`bursar_usage_events`"
            self._INSERT_COLUMNS = ("tenant_id", "event_at", "charge_id", "outbox_event_id")

        def _result_rows(self, result):
            return result.result_rows

    store = Store()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert cutover._clickhouse_has_earlier_rows(store, cutoff)
    assert cutover._clickhouse_count(store, cutoff) == 2
    page = cutover._clickhouse_page(store, cutoff, None, None)
    assert page == [_normalize(row)]
    assert cutover._clickhouse_item(store, row) == row
    with pytest.raises(RuntimeError, match="duplicate ClickHouse usage identity"):
        cutover._clickhouse_item(store, row)
    assert all(parameters["tenant"] == TENANT_ID for _, parameters in store._client.queries)


def test_drain_completes_optional_events_and_exports_payload_and_pointer_rows(monkeypatch):
    pointer_row = _usage_row() | {
        "charge_id": "00000000-0000-0000-0000-000000000005",
        "request_digest": "a" * 64,
    }
    full_payload = _usage_row() | {"delivery_required": True, "request_digest": "\\x" + "a" * 64}
    optional = SimpleNamespace(
        payload_version=1,
        payload={"delivery_required": False},
        aggregate_id=_usage_row()["charge_id"],
        event_id="optional-event",
    )
    full = SimpleNamespace(
        payload_version=1,
        payload=full_payload,
        aggregate_id=_usage_row()["charge_id"],
        event_id="full-event",
    )
    pointer = SimpleNamespace(
        payload_version=1,
        payload={"delivery_required": True},
        aggregate_id=pointer_row["charge_id"],
        event_id="pointer-event",
    )
    events = [optional, full, pointer]
    completed = []
    writes = []
    repositories = []

    class Repository:
        def __init__(self, query, _tenant):
            self.query = query
            self.claimed = False
            self.query("SELECT fixture", [])
            repositories.append(self)

        def claim(self, *_args):
            if self.claimed:
                return []
            self.claimed = True
            return events

        def complete(self, event):
            completed.append(event.event_id)
            return True

        def get_usage_charge(self, charge_id):
            return UsageChargeExport.model_validate(pointer_row) if charge_id == pointer_row["charge_id"] else None

    class Store:
        def write_usage(self, usage, event_id):
            writes.append((usage, event_id))

    monkeypatch.setattr(cutover, "PostgresStorageRepository", Repository)
    cutover._drain(_CutoverConnection(_CutoverCursor("postgres", _usage_row())), Store(), TENANT_ID, "test")

    assert completed == ["optional-event", "full-event", "pointer-event"]
    assert [event_id for _, event_id in writes] == ["full-event", "pointer-event"]
    assert writes[0][0].request_digest == "a" * 64
    assert len(repositories) == 1


def test_drain_rolls_back_query_failure_and_rejects_unsupported_payload(monkeypatch):
    connection = _CutoverConnection(_CutoverCursor("postgres", _usage_row(), fail_statement="SELECT fixture"))

    class QueryRepository:
        def __init__(self, query, _tenant):
            query("SELECT fixture", [])

    monkeypatch.setattr(cutover, "PostgresStorageRepository", QueryRepository)
    with pytest.raises(RuntimeError, match="query failed"):
        cutover._drain(connection, SimpleNamespace(write_usage=lambda *_args: None), TENANT_ID, "test")
    assert any(statement == "ROLLBACK" for statement in connection._cursor.statements)

    event = SimpleNamespace(payload_version=2, payload={}, aggregate_id="charge", event_id="event")

    class UnsupportedRepository:
        def __init__(self, _query, _tenant):
            self.claimed = False

        def claim(self, *_args):
            if self.claimed:
                return []
            self.claimed = True
            return [event]

    monkeypatch.setattr(cutover, "PostgresStorageRepository", UnsupportedRepository)
    with pytest.raises(RuntimeError, match="unsupported usage outbox payload version"):
        cutover._drain(
            _CutoverConnection(_CutoverCursor("postgres", _usage_row())),
            SimpleNamespace(),
            TENANT_ID,
            "test",
        )
