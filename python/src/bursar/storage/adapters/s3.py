"""S3 billing archive mirroring JavaScript ``storage/adapters/s3.ts``."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from bursar.storage.ports import (
    BillingEventPayloadExport,
    BillingPayloadArchiveResult,
)


class _S3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class S3Credentials(_S3Model):
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


class S3BillingArchiveOptions(_S3Model):
    bucket: str
    region: str
    credentials: S3Credentials
    endpoint: str | None = None
    force_path_style: bool = False
    prefix: str = "bursar"


def _require_nonempty(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    return normalized


class S3BillingArchive:
    """Archive received billing webhook envelopes under deterministic keys."""

    def __init__(self, options: S3BillingArchiveOptions) -> None:
        self._bucket = _require_nonempty(options.bucket, "S3 bucket")
        self._region = _require_nonempty(options.region, "S3 region")
        self._credentials = S3Credentials(
            access_key_id=_require_nonempty(options.credentials.access_key_id, "S3 access key ID"),
            secret_access_key=_require_nonempty(options.credentials.secret_access_key, "S3 secret access key"),
            session_token=(
                _require_nonempty(options.credentials.session_token, "S3 session token")
                if options.credentials.session_token
                else None
            ),
        )
        self._endpoint = _require_nonempty(options.endpoint, "S3 endpoint") if options.endpoint else None
        self._force_path_style = options.force_path_style
        self._prefix = options.prefix.strip("/")
        self._client: Any | None = None
        self._client_lock = threading.Lock()

    def archive(self, event: BillingEventPayloadExport) -> BillingPayloadArchiveResult:
        if event.envelope is None:
            msg = f"Billing event {event.event_id} has no PostgreSQL payload to archive"
            raise RuntimeError(msg)
        try:
            received_at = datetime.fromisoformat(event.received_at)
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
                "tenants",
                event.tenant_id,
                "billing-events",
                day,
                f"{event.event_id}.json",
            )
            if part
        )
        document = {
            "schema": "bursar.billing-event-envelope.v1",
            "tenantId": event.tenant_id,
            "eventId": event.event_id,
            "provider": event.provider,
            "providerEnvironment": event.provider_environment,
            "providerEventId": event.provider_event_id,
            "eventType": event.event_type,
            "receivedAt": event.received_at,
            "completedAt": event.completed_at,
            "envelope": event.envelope,
        }
        result = self._get_client().put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(document, separators=(",", ":")).encode(),
            ContentType="application/json",
            Metadata={
                "bursar-tenant-id": event.tenant_id,
                "bursar-event-id": event.event_id,
                "bursar-provider": event.provider,
                "bursar-environment": event.provider_environment,
            },
        )
        version_id = result.get("VersionId")
        return BillingPayloadArchiveResult(
            key=key,
            version_id=str(version_id) if version_id is not None else None,
        )

    def close(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                self._client = boto3.client(
                    "s3",
                    region_name=self._region,
                    endpoint_url=self._endpoint,
                    aws_access_key_id=self._credentials.access_key_id,
                    aws_secret_access_key=self._credentials.secret_access_key,
                    aws_session_token=self._credentials.session_token,
                    config=Config(s3={"addressing_style": ("path" if self._force_path_style else "auto")}),
                )
        return self._client
