export interface OutboxEvent {
  eventId: string;
  tenantId: string;
  topic: string;
  aggregateType: string;
  aggregateId: string;
  payloadVersion: number;
  payload: Record<string, unknown>;
  claimToken: string;
  attemptCount: number;
  createdAt: string;
}

export interface OutboxStore {
  claim(topics: readonly string[], limit: number, leaseSeconds: number): Promise<OutboxEvent[]>;
  complete(event: OutboxEvent): Promise<boolean>;
  fail(
    event: OutboxEvent,
    error: string,
    retryDelaySeconds: number,
    attemptLimit: number,
  ): Promise<boolean>;
}

export interface OutboxHandler {
  readonly topics: readonly string[];
  handle(event: OutboxEvent): Promise<void>;
}

export interface UsageChargeExport {
  tenantId: string;
  chargeId: string;
  accountId: string;
  subjectId: string;
  operation: string;
  feature: string | null;
  model: string | null;
  region: string | null;
  measures: Record<string, unknown>;
  dimensions: Record<string, unknown>;
  metadata: Record<string, unknown>;
  requested: string;
  charged: string;
  allowanceRequested: string;
  allowanceCovered: string;
  catalogRevisionId: string | null;
  planId: string | null;
  rateCardKey: string | null;
  pricingSnapshot: Record<string, unknown>;
  ledgerEntryId: string | null;
  correctionOfChargeId: string | null;
  idempotencyKey: string;
  requestDigest: string;
  eventAt: string;
  createdAt: string;
}

export interface BillingEventPayloadExport {
  tenantId: string;
  eventId: string;
  provider: string;
  providerEnvironment: string;
  providerEventId: string;
  eventType: string;
  status: string;
  receivedAt: string;
  completedAt: string | null;
  envelope: Record<string, unknown> | null;
  objectKey: string | null;
  objectVersion: string | null;
  archivedAt: string | null;
}

export interface UsageEventSink {
  initialize?(): Promise<void>;
  writeUsage(event: UsageChargeExport, outboxEventId: string): Promise<void>;
}

export interface BillingPayloadArchiveResult {
  key: string;
  versionId: string | null;
}

export interface BillingPayloadArchive {
  readonly purgePostgresPayload: boolean;
  archive(event: BillingEventPayloadExport): Promise<BillingPayloadArchiveResult>;
  close?(): Promise<void> | void;
}
