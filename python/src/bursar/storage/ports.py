"""Storage ports mirroring the JavaScript SDK's ``storage/ports.ts``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


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


class OutboxClaimRenewalStore(Protocol):
    def renew(self, event: OutboxEvent, lease_seconds: int) -> bool: ...


class OutboxStore(OutboxClaimRenewalStore, Protocol):
    def claim(self, topics: Sequence[str], limit: int, lease_seconds: int) -> list[OutboxEvent]: ...

    def complete(self, event: OutboxEvent) -> bool: ...

    def fail(
        self,
        event: OutboxEvent,
        error: str,
        retry_delay_seconds: int,
        attempt_limit: int,
    ) -> bool: ...


class OutboxStats(_StorageModel):
    pending_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    oldest_pending_at: str | None


class OutboxDeadLetterCursor(_StorageModel):
    created_at: str = Field(min_length=1)
    event_id: str = Field(pattern=r"^[1-9]\d*$")


class OutboxDeadLetter(_StorageModel):
    event_id: str
    tenant_id: str
    topic: str
    aggregate_type: str
    aggregate_id: str
    payload_version: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    last_error: str | None
    created_at: str
    updated_at: str


class OutboxDeadLetterListOptions(_StorageModel):
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    cursor: OutboxDeadLetterCursor | None = None


class OutboxDeadLetterPage(_StorageModel):
    items: list[OutboxDeadLetter]
    next_cursor: OutboxDeadLetterCursor | None


class OutboxRecoveryStore(OutboxStore, Protocol):
    def stats(self) -> OutboxStats: ...

    def list_dead_letters(
        self,
        options: OutboxDeadLetterListOptions | None = None,
    ) -> OutboxDeadLetterPage: ...

    def requeue(self, event_id: str) -> bool: ...


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


class BatchUsageEventSink(UsageEventSink, Protocol):
    def write_usage_batch(self, entries: Sequence[tuple[UsageChargeExport, str]]) -> None: ...


class UsageProjectionSchema(Protocol):
    """Optional non-mutating compatibility check for caller-managed schemas."""

    def check_schema_compatibility(self) -> None: ...


class BillingPayloadArchiveResult(_StorageModel):
    key: str
    version_id: str | None


class BillingPayloadArchive(Protocol):
    def archive(self, event: BillingEventPayloadExport) -> BillingPayloadArchiveResult: ...
