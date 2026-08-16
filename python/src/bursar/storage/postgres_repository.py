"""PostgreSQL storage repository mirroring JavaScript implementation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from bursar.shared.postgres_types import QueryFn
from bursar.storage.ports import (
    BillingEventPayloadExport,
    OutboxDeadLetter,
    OutboxDeadLetterCursor,
    OutboxDeadLetterListOptions,
    OutboxDeadLetterPage,
    OutboxEvent,
    OutboxStats,
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


def _json_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{context} must be a JSON object"
        raise RuntimeError(msg)
    return value


def _billing_disposition(row: dict[str, Any], context: str) -> Literal["billable", "record_only"]:
    value = _required_string(row, "billing_disposition", context)
    if value == "billable":
        return "billable"
    if value == "record_only":
        return "record_only"
    msg = f"{context}.billing_disposition must be billable or record_only"
    raise RuntimeError(msg)


def _nonnegative_integer(row: dict[str, Any], key: str, context: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes, bytearray)):
        msg = f"{context}.{key} must be a non-negative integer"
        raise RuntimeError(msg)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        msg = f"{context}.{key} must be a non-negative integer"
        raise RuntimeError(msg) from error
    if parsed < 0:
        msg = f"{context}.{key} must be a non-negative integer"
        raise RuntimeError(msg)
    return parsed


def _positive_event_id(value: str) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or value.startswith("0"):
        raise ValueError("outbox event_id must be a positive integer string")
    return value


def _persisted_diagnostic_summary(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("outbox failure summary must be a string")
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("outbox failure summary must contain operation and error codes")
    for part in parts:
        if (
            not part
            or len(part) > 128
            or not part.isascii()
            or not part[0].isalpha()
            or any(not (character.isalnum() or character in "_.-") for character in part)
        ):
            raise ValueError("outbox failure summary contains an invalid diagnostic code")
    return value


def _scalar_boolean(rows: list[Any]) -> bool:
    if len(rows) != 1:
        msg = f"PostgreSQL boolean RPC returned {len(rows)} rows; expected one"
        raise RuntimeError(msg)
    row = _as_row(rows[0], "PostgreSQL boolean RPC")
    if len(row) != 1:
        msg = "PostgreSQL boolean RPC returned an invalid result envelope"
        raise RuntimeError(msg)
    value = next(iter(row.values()))
    if not isinstance(value, bool):
        msg = "PostgreSQL boolean RPC result must be a boolean"
        raise RuntimeError(msg)
    return value


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
                    payload=_json_object(row.get("payload"), "outbox event.payload"),
                    claim_token=_required_string(row, "claim_token", "outbox event"),
                    attempt_count=int(row["attempt_count"]),
                    created_at=_required_string(row, "created_at", "outbox event"),
                )
            )
        return events

    def _assert_event_tenant(self, event: OutboxEvent) -> None:
        if event.tenant_id != self._tenant_id:
            msg = "Outbox event tenant does not match repository tenant"
            raise RuntimeError(msg)

    def renew(self, event: OutboxEvent, lease_seconds: int) -> bool:
        self._assert_event_tenant(event)
        return _scalar_boolean(
            self._query(
                """
                SELECT bursar.renew_tenant_outbox_claim(
                    %s::uuid,
                    %s::bigint,
                    %s::uuid,
                    %s::integer
                )
                """,
                [self._tenant_id, event.event_id, event.claim_token, lease_seconds],
            )
        )

    def complete(self, event: OutboxEvent) -> bool:
        self._assert_event_tenant(event)
        return _scalar_boolean(
            self._query(
                """
                SELECT bursar.complete_tenant_outbox_event(
                    %s::uuid,
                    %s::bigint,
                    %s::uuid
                )
                """,
                [self._tenant_id, event.event_id, event.claim_token],
            )
        )

    def fail(
        self,
        event: OutboxEvent,
        error: str,
        retry_delay_seconds: int,
        attempt_limit: int,
    ) -> bool:
        self._assert_event_tenant(event)
        return _scalar_boolean(
            self._query(
                """
                SELECT bursar.fail_tenant_outbox_event(
                    %s::uuid,
                    %s::bigint,
                    %s::uuid,
                    %s::text,
                    %s::integer,
                    %s::integer
                )
                """,
                [
                    self._tenant_id,
                    event.event_id,
                    event.claim_token,
                    _persisted_diagnostic_summary(error),
                    retry_delay_seconds,
                    attempt_limit,
                ],
            )
        )

    def stats(self) -> OutboxStats:
        rows = self._query(
            "SELECT * FROM bursar.get_outbox_stats(%s::uuid)",
            [self._tenant_id],
        )
        if len(rows) != 1:
            msg = f"get_outbox_stats returned {len(rows)} rows; expected one"
            raise RuntimeError(msg)
        row = _as_row(rows[0], "get_outbox_stats")
        return OutboxStats(
            pending_count=_nonnegative_integer(row, "pending_count", "outbox stats"),
            processing_count=_nonnegative_integer(row, "processing_count", "outbox stats"),
            delivered_count=_nonnegative_integer(row, "delivered_count", "outbox stats"),
            dead_letter_count=_nonnegative_integer(row, "dead_letter_count", "outbox stats"),
            oldest_pending_at=_optional_string(row, "oldest_pending_at"),
        )

    def list_dead_letters(
        self,
        options: OutboxDeadLetterListOptions | None = None,
    ) -> OutboxDeadLetterPage:
        effective = options or OutboxDeadLetterListOptions()
        rows = self._query(
            """
            SELECT * FROM bursar.list_outbox_dead_letters(
                %s::uuid,
                %s::timestamptz,
                %s::bigint,
                %s::integer
            )
            """,
            [
                self._tenant_id,
                effective.cursor.created_at if effective.cursor is not None else None,
                effective.cursor.event_id if effective.cursor is not None else None,
                effective.limit,
            ],
        )
        dead_letters: list[OutboxDeadLetter] = []
        for raw in rows:
            row = _as_row(raw, "list_outbox_dead_letters")
            dead_letters.append(
                OutboxDeadLetter(
                    event_id=_positive_event_id(_required_string(row, "event_id", "outbox dead letter")),
                    tenant_id=_required_string(row, "tenant_id", "outbox dead letter"),
                    topic=_required_string(row, "topic", "outbox dead letter"),
                    aggregate_type=_required_string(row, "aggregate_type", "outbox dead letter"),
                    aggregate_id=_required_string(row, "aggregate_id", "outbox dead letter"),
                    payload_version=_nonnegative_integer(row, "payload_version", "outbox dead letter"),
                    attempt_count=_nonnegative_integer(row, "attempt_count", "outbox dead letter"),
                    last_error=_optional_string(row, "last_error"),
                    created_at=_required_string(row, "created_at", "outbox dead letter"),
                    updated_at=_required_string(row, "updated_at", "outbox dead letter"),
                )
            )
        has_more = len(dead_letters) > effective.limit
        items = dead_letters[: effective.limit] if has_more else dead_letters
        last = items[-1] if items else None
        return OutboxDeadLetterPage(
            items=items,
            next_cursor=(
                OutboxDeadLetterCursor(created_at=last.created_at, event_id=last.event_id)
                if has_more and last is not None
                else None
            ),
        )

    def requeue(self, event_id: str) -> bool:
        normalized_event_id = _positive_event_id(event_id)
        return _scalar_boolean(
            self._query(
                "SELECT bursar.requeue_outbox_dead_letter(%s::uuid, %s::bigint)",
                [self._tenant_id, normalized_event_id],
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
            measures=_json_object(row.get("measures"), "usage charge export.measures"),
            dimensions=_json_object(row.get("dimensions"), "usage charge export.dimensions"),
            metadata=_json_object(row.get("metadata"), "usage charge export.metadata"),
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
            billing_disposition=_billing_disposition(row, "usage charge export"),
            catalog_revision_id=_optional_string(row, "catalog_revision_id"),
            plan_id=_optional_string(row, "plan_id"),
            rate_card_key=_optional_string(row, "rate_card_key"),
            pricing_snapshot=_json_object(
                row.get("pricing_snapshot"),
                "usage charge export.pricing_snapshot",
            ),
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
            envelope=(_json_object(envelope, "billing payload export.envelope") if envelope is not None else None),
            object_key=_optional_string(row, "object_key"),
            object_version=_optional_string(row, "object_version"),
            archived_at=_optional_string(row, "archived_at"),
        )

    def archive_billing_event_payload(
        self,
        event_id: str,
        object_key: str,
        object_version: str | None,
    ) -> bool:
        return _scalar_boolean(
            self._query(
                """
                SELECT bursar.archive_billing_event_payload(
                    %s::uuid,
                    %s::text,
                    %s::text
                )
                """,
                [event_id, object_key, object_version],
            )
        )
