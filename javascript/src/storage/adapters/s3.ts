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

/** Structural S3 client surface accepted by the archive adapter. */
export interface S3ArchiveClient {
  send(command: unknown): Promise<{ VersionId?: string }>;
  destroy?(): void;
}

export type S3ArchiveClientFactory = () => S3ArchiveClient | Promise<S3ArchiveClient>;

/**
 * Safe per-object controls. Bucket policy, ACLs, object-lock policy, and object
 * ownership remain the embedding application's responsibility.
 */
export interface S3PutObjectOptions {
  ServerSideEncryption?: "AES256" | "aws:backup" | "aws:fsx" | "aws:kms" | "aws:kms:dsse";
  SSEKMSKeyId?: string;
  SSEKMSEncryptionContext?: string;
  BucketKeyEnabled?: boolean;
  ChecksumAlgorithm?:
    | "CRC32"
    | "CRC32C"
    | "CRC64NVME"
    | "MD5"
    | "SHA1"
    | "SHA256"
    | "SHA512"
    | "XXHASH128"
    | "XXHASH3"
    | "XXHASH64";
  ChecksumCRC32?: string;
  ChecksumCRC32C?: string;
  ChecksumCRC64NVME?: string;
  ChecksumSHA1?: string;
  ChecksumSHA256?: string;
  ChecksumSHA512?: string;
  ChecksumMD5?: string;
  ChecksumXXHASH64?: string;
  ChecksumXXHASH3?: string;
  ChecksumXXHASH128?: string;
}

export interface S3BillingArchiveOptions {
  bucket: string;
  /** Omit to use the AWS SDK's normal region provider chain. */
  region?: string;
  /** Omit to use the AWS SDK's normal credential provider chain. */
  credentials?: S3Credentials;
  endpoint?: string;
  forcePathStyle?: boolean;
  prefix?: string;
  /** Use an already configured client. It is not destroyed by default. */
  client?: S3ArchiveClient;
  /** Lazily construct a client. Factory-created clients are owned by default. */
  clientFactory?: S3ArchiveClientFactory;
  /** Override client ownership. Defaults to false for `client`, true otherwise. */
  ownsClient?: boolean;
  /** Optional encryption and checksum controls applied to every archived object. */
  putObject?: Readonly<S3PutObjectOptions>;
}

function requireNonEmpty(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new TypeError(`${name} must not be empty`);
  return normalized;
}

function normalizePrefix(prefix: string | undefined): string {
  return (prefix ?? "bursar").replace(/^\/+|\/+$/g, "");
}

/** Archives received billing webhook envelopes under deterministic keys. */
export class S3BillingArchive implements BillingPayloadArchive {
  private readonly bucket: string;
  private readonly prefix: string;
  private readonly clientFactory: S3ArchiveClientFactory;
  private readonly ownsClient: boolean;
  private readonly putObject: Readonly<S3PutObjectOptions>;
  private clientPromise: Promise<S3ArchiveClient> | null = null;

  constructor(options: S3BillingArchiveOptions) {
    if (options.client && options.clientFactory) {
      throw new TypeError("S3 client and clientFactory are mutually exclusive");
    }
    this.bucket = requireNonEmpty(options.bucket, "S3 bucket");
    this.prefix = normalizePrefix(options.prefix);
    this.ownsClient = options.ownsClient ?? options.client === undefined;
    this.putObject = { ...options.putObject };

    if (options.client) {
      this.clientFactory = () => options.client as S3ArchiveClient;
      return;
    }
    if (options.clientFactory) {
      this.clientFactory = options.clientFactory;
      return;
    }

    const region = options.region ? requireNonEmpty(options.region, "S3 region") : undefined;
    const endpoint = options.endpoint
      ? requireNonEmpty(options.endpoint, "S3 endpoint")
      : undefined;
    const credentials = options.credentials
      ? {
          accessKeyId: requireNonEmpty(options.credentials.accessKeyId, "S3 access key ID"),
          secretAccessKey: requireNonEmpty(
            options.credentials.secretAccessKey,
            "S3 secret access key",
          ),
          ...(options.credentials.sessionToken
            ? {
                sessionToken: requireNonEmpty(options.credentials.sessionToken, "S3 session token"),
              }
            : {}),
        }
      : undefined;
    const forcePathStyle = options.forcePathStyle ?? false;
    this.clientFactory = async () => {
      const { S3Client } = await import("@aws-sdk/client-s3");
      return new S3Client({
        ...(region ? { region } : {}),
        ...(endpoint ? { endpoint } : {}),
        forcePathStyle,
        ...(credentials ? { credentials } : {}),
      });
    };
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
        ...this.putObject,
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
    if (this.ownsClient) client.destroy?.();
  }

  private getClient(): Promise<S3ArchiveClient> {
    if (!this.clientPromise) {
      this.clientPromise = Promise.resolve()
        .then(this.clientFactory)
        .then((client) => {
          if (
            typeof client !== "object" ||
            client === null ||
            typeof client.send !== "function" ||
            (client.destroy !== undefined && typeof client.destroy !== "function")
          ) {
            throw new TypeError("S3 client must provide send() and an optional destroy()");
          }
          return client;
        })
        .catch((error: unknown) => {
          this.clientPromise = null;
          throw error;
        });
    }
    return this.clientPromise;
  }
}
