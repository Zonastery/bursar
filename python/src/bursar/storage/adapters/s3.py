"""S3 billing archive mirroring JavaScript ``storage/adapters/s3.ts``."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

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


class S3PutObjectOptions(_S3Model):
    """Safe per-object encryption and checksum controls."""

    server_side_encryption: str | None = None
    sse_kms_key_id: str | None = None
    sse_kms_encryption_context: str | None = None
    bucket_key_enabled: bool | None = None
    checksum_algorithm: str | None = None
    checksum_crc32: str | None = None
    checksum_crc32c: str | None = None
    checksum_crc64_nvme: str | None = None
    checksum_sha1: str | None = None
    checksum_sha256: str | None = None
    checksum_sha512: str | None = None
    checksum_md5: str | None = None
    checksum_xxhash64: str | None = None
    checksum_xxhash3: str | None = None
    checksum_xxhash128: str | None = None


class S3BillingArchiveOptions(_S3Model):
    bucket: str
    region: str | None = None
    credentials: S3Credentials | None = None
    endpoint: str | None = None
    force_path_style: bool = False
    prefix: str = "bursar"
    client: SkipValidation[Any] | None = None
    client_factory: SkipValidation[Callable[[], Any]] | None = None
    owns_client: bool | None = None
    put_object: S3PutObjectOptions | Mapping[str, Any] = Field(default_factory=S3PutObjectOptions)


def _require_nonempty(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    return normalized


class S3BillingArchive:
    """Archive received billing webhook envelopes under deterministic keys."""

    def __init__(self, options: S3BillingArchiveOptions) -> None:
        if options.client is not None and options.client_factory is not None:
            msg = "S3 client and client_factory are mutually exclusive"
            raise ValueError(msg)
        self._bucket = _require_nonempty(options.bucket, "S3 bucket")
        self._region = _require_nonempty(options.region, "S3 region") if options.region else None
        self._credentials = (
            S3Credentials(
                access_key_id=_require_nonempty(options.credentials.access_key_id, "S3 access key ID"),
                secret_access_key=_require_nonempty(options.credentials.secret_access_key, "S3 secret access key"),
                session_token=(
                    _require_nonempty(options.credentials.session_token, "S3 session token")
                    if options.credentials.session_token
                    else None
                ),
            )
            if options.credentials is not None
            else None
        )
        self._endpoint = _require_nonempty(options.endpoint, "S3 endpoint") if options.endpoint else None
        self._force_path_style = options.force_path_style
        self._prefix = options.prefix.strip("/")
        self._client: Any | None = options.client
        self._client_factory = (lambda: options.client) if options.client is not None else options.client_factory
        self._owns_client = options.owns_client if options.owns_client is not None else options.client is None
        put_object = (
            options.put_object
            if isinstance(options.put_object, S3PutObjectOptions)
            else S3PutObjectOptions.model_validate(options.put_object)
        )
        self._put_object = self._put_object_options(put_object)
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
            **self._put_object,
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
        if client is not None and self._owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = self._client_factory() if self._client_factory is not None else self._create_client()
        return self._client

    def _create_client(self) -> Any:
        import boto3
        from botocore.config import Config

        kwargs: dict[str, Any] = {
            "config": Config(s3={"addressing_style": ("path" if self._force_path_style else "auto")})
        }
        if self._region is not None:
            kwargs["region_name"] = self._region
        if self._endpoint is not None:
            kwargs["endpoint_url"] = self._endpoint
        if self._credentials is not None:
            kwargs.update(
                aws_access_key_id=self._credentials.access_key_id,
                aws_secret_access_key=self._credentials.secret_access_key,
                aws_session_token=self._credentials.session_token,
            )
        return boto3.client("s3", **kwargs)

    @staticmethod
    def _put_object_options(options: S3PutObjectOptions) -> dict[str, Any]:
        field_names = {
            "server_side_encryption": "ServerSideEncryption",
            "sse_kms_key_id": "SSEKMSKeyId",
            "sse_kms_encryption_context": "SSEKMSEncryptionContext",
            "bucket_key_enabled": "BucketKeyEnabled",
            "checksum_algorithm": "ChecksumAlgorithm",
            "checksum_crc32": "ChecksumCRC32",
            "checksum_crc32c": "ChecksumCRC32C",
            "checksum_crc64_nvme": "ChecksumCRC64NVME",
            "checksum_sha1": "ChecksumSHA1",
            "checksum_sha256": "ChecksumSHA256",
            "checksum_sha512": "ChecksumSHA512",
            "checksum_md5": "ChecksumMD5",
            "checksum_xxhash64": "ChecksumXXHASH64",
            "checksum_xxhash3": "ChecksumXXHASH3",
            "checksum_xxhash128": "ChecksumXXHASH128",
        }
        request: dict[str, Any] = {}
        for field_name, request_name in field_names.items():
            value = getattr(options, field_name)
            if value is None:
                continue
            request[request_name] = _require_nonempty(value, request_name) if isinstance(value, str) else value
        return request
