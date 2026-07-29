"""S3 billing archive mirroring JavaScript ``storage/adapters/s3.ts``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from bursar.storage.ports import (
    BillingEventPayloadExport,
    BillingPayloadArchiveResult,
)


@dataclass(frozen=True, slots=True)
class S3PutObjectRequest:
    bucket: str
    key: str
    body: bytes
    content_type: str
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class S3PutObjectResult:
    version_id: str | None = None


class S3PutObject(Protocol):
    def __call__(self, request: S3PutObjectRequest) -> S3PutObjectResult: ...


@dataclass(frozen=True, slots=True)
class S3BillingArchiveOptions:
    bucket: str
    put_object: S3PutObject
    prefix: str = "bursar"
    purge_postgres_payload: bool = True


def _require_nonempty(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    return normalized


class S3BillingArchive:
    """Archive completed billing webhook envelopes under deterministic keys."""

    def __init__(self, options: S3BillingArchiveOptions) -> None:
        self._bucket = _require_nonempty(options.bucket, "S3 bucket")
        self._prefix = options.prefix.strip("/")
        self._put_object = options.put_object
        self._purge_postgres_payload = options.purge_postgres_payload

    @property
    def purge_postgres_payload(self) -> bool:
        return self._purge_postgres_payload

    def archive(self, event: BillingEventPayloadExport) -> BillingPayloadArchiveResult:
        if event.envelope is None:
            msg = f"Billing event {event.event_id} has no PostgreSQL payload to archive"
            raise RuntimeError(msg)
        try:
            received_at = datetime.fromisoformat(event.received_at.replace("Z", "+00:00"))
        except ValueError as error:
            msg = f"Billing event {event.event_id} has an invalid received_at timestamp"
            raise RuntimeError(msg) from error
        if received_at.tzinfo is None:
            msg = f"Billing event {event.event_id} has an invalid received_at timestamp"
            raise RuntimeError(msg)

        day = received_at.astimezone(UTC).strftime("%Y/%m/%d")
        key = "/".join(
            part
            for part in (
                self._prefix,
                "billing-events",
                day,
                f"{event.event_id}.json",
            )
            if part
        )
        document = {
            "schema": "bursar.billing-event-envelope.v1",
            "eventId": event.event_id,
            "provider": event.provider,
            "providerEnvironment": event.provider_environment,
            "providerEventId": event.provider_event_id,
            "eventType": event.event_type,
            "receivedAt": event.received_at,
            "completedAt": event.completed_at,
            "envelope": event.envelope,
        }
        result = self._put_object(
            S3PutObjectRequest(
                bucket=self._bucket,
                key=key,
                body=json.dumps(document, separators=(",", ":")).encode(),
                content_type="application/json",
                metadata={
                    "bursar-event-id": event.event_id,
                    "bursar-provider": event.provider,
                    "bursar-environment": event.provider_environment,
                },
            )
        )
        return BillingPayloadArchiveResult(key=key, version_id=result.version_id)
