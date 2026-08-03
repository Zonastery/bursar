"""Storage ports mirroring the JavaScript SDK's ``storage/ports.ts``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


class _StorageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutboxEvent(_StorageModel):
    event_id: str
    tenant_id: str
    topic: str
    aggregate_type: str
    aggregate_id: str
    payload_version: int
    payload: dict[str, Any]
    claim_token: str
    attempt_count: int
    created_at: str


class OutboxStore(Protocol):
    def claim(self, topics: Sequence[str], limit: int, lease_seconds: int) -> list[OutboxEvent]: ...

    def complete(self, event: OutboxEvent) -> bool: ...

    def fail(
        self,
        event: OutboxEvent,
        error: str,
        retry_delay_seconds: int,
        attempt_limit: int,
    ) -> bool: ...


class OutboxHandler(Protocol):
    @property
    def topics(self) -> Sequence[str]: ...

    def handle(self, event: OutboxEvent) -> None: ...


class UsageChargeExport(_StorageModel):
    tenant_id: str
    charge_id: str
    account_id: str
    subject_id: str
    operation: str
    feature: str | None
    model: str | None
    region: str | None
    measures: dict[str, Any]
    dimensions: dict[str, Any]
    metadata: dict[str, Any]
    requested: str
    charged: str
    allowance_requested: str
    allowance_covered: str
    billing_disposition: Literal["billable", "record_only"] = "billable"
    catalog_revision_id: str | None
    plan_id: str | None
    rate_card_key: str | None
    pricing_snapshot: dict[str, Any]
    ledger_entry_id: str | None
    correction_of_charge_id: str | None
    idempotency_key: str
    request_digest: str
    event_at: str
    created_at: str


class BillingEventPayloadExport(_StorageModel):
    tenant_id: str
    event_id: str
    provider: str
    provider_environment: str
    provider_event_id: str
    event_type: str
    status: str
    received_at: str
    completed_at: str | None
    envelope: dict[str, Any] | None
    object_key: str | None = None
    object_version: str | None = None
    archived_at: str | None = None


class UsageEventSink(Protocol):
    def write_usage(self, event: UsageChargeExport, outbox_event_id: str) -> None: ...


class BillingPayloadArchiveResult(_StorageModel):
    key: str
    version_id: str | None


class BillingPayloadArchive(Protocol):
    @property
    def purge_postgres_payload(self) -> bool: ...

    def archive(self, event: BillingEventPayloadExport) -> BillingPayloadArchiveResult: ...
