import type {
  BillingEventPayloadExport,
  BillingPayloadArchive,
  BillingPayloadArchiveResult,
} from "../ports.js";

export interface S3Credentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken?: string;
}

export interface S3BillingArchiveOptions {
  bucket: string;
  region: string;
  credentials: S3Credentials;
  endpoint?: string;
  forcePathStyle?: boolean;
  prefix?: string;
  /**
   * Delete the raw envelope from PostgreSQL after S3 confirms the upload.
   * Defaults to true. The event and S3 pointer stay in PostgreSQL.
   */
  purgePostgresPayload?: boolean;
}

function requireNonEmpty(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new TypeError(`${name} must not be empty`);
  return normalized;
}

function normalizePrefix(prefix: string | undefined): string {
  return (prefix ?? "bursar").replace(/^\/+|\/+$/g, "");
}

/** Archives completed billing webhook envelopes under deterministic keys. */
export class S3BillingArchive implements BillingPayloadArchive {
  readonly purgePostgresPayload: boolean;
  private readonly bucket: string;
  private readonly region: string;
  private readonly credentials: S3Credentials;
  private readonly endpoint: string | undefined;
  private readonly forcePathStyle: boolean;
  private readonly prefix: string;
  private clientPromise: Promise<import("@aws-sdk/client-s3").S3Client> | null = null;

  constructor(options: S3BillingArchiveOptions) {
    this.bucket = requireNonEmpty(options.bucket, "S3 bucket");
    this.region = requireNonEmpty(options.region, "S3 region");
    this.credentials = {
      accessKeyId: requireNonEmpty(options.credentials.accessKeyId, "S3 access key ID"),
      secretAccessKey: requireNonEmpty(options.credentials.secretAccessKey, "S3 secret access key"),
      ...(options.credentials.sessionToken
        ? { sessionToken: requireNonEmpty(options.credentials.sessionToken, "S3 session token") }
        : {}),
    };
    this.endpoint = options.endpoint ? requireNonEmpty(options.endpoint, "S3 endpoint") : undefined;
    this.forcePathStyle = options.forcePathStyle ?? false;
    this.prefix = normalizePrefix(options.prefix);
    this.purgePostgresPayload = options.purgePostgresPayload ?? true;
  }

  async archive(event: BillingEventPayloadExport): Promise<BillingPayloadArchiveResult> {
    if (!event.envelope) {
      throw new Error(`Billing event ${event.eventId} has no PostgreSQL payload to archive`);
    }
    const receivedAt = new Date(event.receivedAt);
    if (Number.isNaN(receivedAt.getTime()) || !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(event.receivedAt)) {
      throw new Error(`Billing event ${event.eventId} has an invalid receivedAt timestamp`);
    }

    const day = receivedAt.toISOString().slice(0, 10).replaceAll("-", "/");
    const key = [
      this.prefix,
      "tenants",
      event.tenantId,
      "billing-events",
      day,
      `${event.eventId}.json`,
    ]
      .filter(Boolean)
      .join("/");
    const { PutObjectCommand } = await import("@aws-sdk/client-s3");
    const client = await this.getClient();
    const result = await client.send(
      new PutObjectCommand({
        Bucket: this.bucket,
        Key: key,
        Body: new TextEncoder().encode(
          JSON.stringify({
            schema: "bursar.billing-event-envelope.v1",
            tenantId: event.tenantId,
            eventId: event.eventId,
            provider: event.provider,
            providerEnvironment: event.providerEnvironment,
            providerEventId: event.providerEventId,
            eventType: event.eventType,
            receivedAt: event.receivedAt,
            completedAt: event.completedAt,
            envelope: event.envelope,
          }),
        ),
        ContentType: "application/json",
        Metadata: {
          "bursar-tenant-id": event.tenantId,
          "bursar-event-id": event.eventId,
          "bursar-provider": event.provider,
          "bursar-environment": event.providerEnvironment,
        },
      }),
    );
    return { key, versionId: result.VersionId ?? null };
  }

  async close(): Promise<void> {
    if (!this.clientPromise) return;
    const client = await this.clientPromise;
    this.clientPromise = null;
    client.destroy();
  }

  private getClient(): Promise<import("@aws-sdk/client-s3").S3Client> {
    if (!this.clientPromise) {
      this.clientPromise = import("@aws-sdk/client-s3").then(
        ({ S3Client }) =>
          new S3Client({
            region: this.region,
            endpoint: this.endpoint,
            forcePathStyle: this.forcePathStyle,
            credentials: this.credentials,
          }),
      );
    }
    return this.clientPromise;
  }
}
