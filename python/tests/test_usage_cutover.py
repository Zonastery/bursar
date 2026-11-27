from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from bursar.storage.cutover import _digest, _normalize


def test_normalization_compares_driver_types_without_losing_details():
    row = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
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
