import type {
  BillingEventPayloadExport,
  BillingPayloadArchive,
  BillingPayloadArchiveResult,
} from "../ports.js";

export interface S3PutObjectRequest {
  bucket: string;
  key: string;
  body: Uint8Array;
  contentType: "application/json";
  metadata: Record<string, string>;
}

export interface S3PutObjectResult {
  versionId?: string | null;
}

export type S3PutObject = (request: S3PutObjectRequest) => Promise<S3PutObjectResult>;

export interface S3BillingArchiveOptions {
  bucket: string;
  putObject: S3PutObject;
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
  private readonly prefix: string;
  private readonly putObject: S3PutObject;

  constructor(options: S3BillingArchiveOptions) {
    this.bucket = requireNonEmpty(options.bucket, "S3 bucket");
    this.prefix = normalizePrefix(options.prefix);
    this.putObject = options.putObject;
    this.purgePostgresPayload = options.purgePostgresPayload ?? true;
  }

  async archive(event: BillingEventPayloadExport): Promise<BillingPayloadArchiveResult> {
    if (!event.envelope) {
      throw new Error(`Billing event ${event.eventId} has no PostgreSQL payload to archive`);
    }
    const receivedAt = new Date(event.receivedAt);
    if (Number.isNaN(receivedAt.getTime())) {
      throw new Error(`Billing event ${event.eventId} has an invalid receivedAt timestamp`);
    }

    const day = receivedAt.toISOString().slice(0, 10).replaceAll("-", "/");
    const key = [this.prefix, "billing-events", day, `${event.eventId}.json`]
      .filter(Boolean)
      .join("/");
    const result = await this.putObject({
      bucket: this.bucket,
      key,
      body: new TextEncoder().encode(
        JSON.stringify({
          schema: "bursar.billing-event-envelope.v1",
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
      contentType: "application/json",
      metadata: {
        "bursar-event-id": event.eventId,
        "bursar-provider": event.provider,
        "bursar-environment": event.providerEnvironment,
      },
    });
    return { key, versionId: result.versionId ?? null };
  }
}
