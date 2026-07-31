"""PostgreSQL storage repository mirroring JavaScript implementation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from bursar.shared.postgres_types import QueryFn
from bursar.storage.ports import (
    BillingEventPayloadExport,
    OutboxEvent,
    UsageChargeExport,
)


def _as_row(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{context} returned an invalid row"
        raise RuntimeError(msg)
    return value


def _required_string(row: dict[str, Any], key: str, context: str) -> str:
    value = row.get(key)
    if value is None or str(value) == "":
        msg = f"{context} is missing {key}"
        raise RuntimeError(msg)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _optional_string(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _scalar_boolean(rows: list[Any]) -> bool:
    if len(rows) != 1:
        return False
    row = _as_row(rows[0], "PostgreSQL boolean RPC")
    return len(row) == 1 and next(iter(row.values())) is True


class PostgresStorageRepository:
    def __init__(self, query: QueryFn, tenant_id: str | UUID) -> None:
        self._query = query
        self._tenant_id = str(UUID(str(tenant_id)))

    def claim(
        self,
        topics: Sequence[str],
        limit: int,
        lease_seconds: int,
    ) -> list[OutboxEvent]:
        rows = self._query(
            """
            SELECT * FROM bursar.claim_outbox_events(
                %s::uuid,
                %s::integer,
                %s::integer,
                %s::text[]
            )
            """,
            [self._tenant_id, limit, lease_seconds, list(topics)],
        )
        events: list[OutboxEvent] = []
        for raw in rows:
            row = _as_row(raw, "claim_outbox_events")
            events.append(
                OutboxEvent(
                    event_id=_required_string(row, "event_id", "outbox event"),
                    tenant_id=_required_string(row, "tenant_id", "outbox event"),
                    topic=_required_string(row, "topic", "outbox event"),
                    aggregate_type=_required_string(row, "aggregate_type", "outbox event"),
                    aggregate_id=_required_string(row, "aggregate_id", "outbox event"),
                    payload_version=int(row["payload_version"]),
                    payload=_json_object(row.get("payload")),
                    claim_token=_required_string(row, "claim_token", "outbox event"),
                    attempt_count=int(row["attempt_count"]),
                    created_at=_required_string(row, "created_at", "outbox event"),
                )
            )
        return events

    def complete(self, event: OutboxEvent) -> bool:
        return _scalar_boolean(
            self._query(
                "SELECT bursar.complete_outbox_event(%s::bigint, %s::uuid)",
                [event.event_id, event.claim_token],
            )
        )

    def fail(
        self,
        event: OutboxEvent,
        error: str,
        retry_delay_seconds: int,
        attempt_limit: int,
    ) -> bool:
        return _scalar_boolean(
            self._query(
                """
                SELECT bursar.fail_outbox_event(
                    %s::bigint,
                    %s::uuid,
                    %s::text,
                    %s::integer,
                    %s::integer
                )
                """,
                [
                    event.event_id,
                    event.claim_token,
                    error,
                    retry_delay_seconds,
                    attempt_limit,
                ],
            )
        )

    def get_usage_charge(self, charge_id: str) -> UsageChargeExport | None:
        rows = self._query(
            "SELECT bursar.export_usage_charge(%s::uuid) AS payload",
            [charge_id],
        )
        value = _as_row(rows[0], "export_usage_charge").get("payload") if rows else None
        if value is None:
            return None
        row = _as_row(value, "export_usage_charge payload")
        if row.get("payload_available") is not True:
            msg = f"Usage charge {charge_id} payload expired before export"
            raise RuntimeError(msg)
        return UsageChargeExport(
            tenant_id=_required_string(row, "tenant_id", "usage charge export"),
            charge_id=_required_string(row, "charge_id", "usage charge export"),
            account_id=_required_string(row, "account_id", "usage charge export"),
            subject_id=_required_string(row, "subject_id", "usage charge export"),
            operation=_required_string(row, "operation", "usage charge export"),
            feature=_optional_string(row, "feature"),
            model=_optional_string(row, "model"),
            region=_optional_string(row, "region"),
            measures=_json_object(row.get("measures")),
            dimensions=_json_object(row.get("dimensions")),
            metadata=_json_object(row.get("metadata")),
            requested=_required_string(row, "requested", "usage charge export"),
            charged=_required_string(row, "charged", "usage charge export"),
            allowance_requested=_required_string(
                row,
                "allowance_requested",
                "usage charge export",
            ),
            allowance_covered=_required_string(
                row,
                "allowance_covered",
                "usage charge export",
            ),
            catalog_revision_id=_optional_string(row, "catalog_revision_id"),
            plan_id=_optional_string(row, "plan_id"),
            rate_card_key=_optional_string(row, "rate_card_key"),
            pricing_snapshot=_json_object(row.get("pricing_snapshot")),
            ledger_entry_id=_optional_string(row, "ledger_entry_id"),
            correction_of_charge_id=_optional_string(row, "correction_of_charge_id"),
            idempotency_key=_required_string(row, "idempotency_key", "usage charge export"),
            request_digest=_required_string(row, "request_digest", "usage charge export"),
            event_at=_required_string(row, "event_at", "usage charge export"),
            created_at=_required_string(row, "created_at", "usage charge export"),
        )

    def get_billing_event_payload(self, event_id: str) -> BillingEventPayloadExport | None:
        rows = self._query(
            "SELECT bursar.export_billing_event_payload(%s::uuid) AS payload",
            [event_id],
        )
        value = _as_row(rows[0], "export_billing_event_payload").get("payload") if rows else None
        if value is None:
            return None
        row = _as_row(value, "export_billing_event_payload payload")
        envelope = row.get("envelope")
        return BillingEventPayloadExport(
            tenant_id=_required_string(row, "tenant_id", "billing payload export"),
            event_id=_required_string(row, "event_id", "billing payload export"),
            provider=_required_string(row, "provider", "billing payload export"),
            provider_environment=_required_string(
                row,
                "provider_environment",
                "billing payload export",
            ),
            provider_event_id=_required_string(
                row,
                "provider_event_id",
                "billing payload export",
            ),
            event_type=_required_string(row, "event_type", "billing payload export"),
            status=_required_string(row, "status", "billing payload export"),
            received_at=_required_string(row, "received_at", "billing payload export"),
            completed_at=_optional_string(row, "completed_at"),
            envelope=_json_object(envelope) if envelope is not None else None,
            object_key=_optional_string(row, "object_key"),
            object_version=_optional_string(row, "object_version"),
            archived_at=_optional_string(row, "archived_at"),
        )

    def archive_billing_event_payload(
        self,
        event_id: str,
        object_key: str,
        object_version: str | None,
        purge_postgres_payload: bool,
    ) -> bool:
        return _scalar_boolean(
            self._query(
                """
                SELECT bursar.archive_billing_event_payload(
                    %s::uuid,
                    %s::text,
                    %s::text,
                    %s::boolean
                )
                """,
                [event_id, object_key, object_version, purge_postgres_payload],
            )
        )
