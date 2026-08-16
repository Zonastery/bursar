import type { JsonObject } from "../shared/json.js";

export interface OutboxEvent {
  eventId: string;
  tenantId: string;
  topic: string;
  aggregateType: string;
  aggregateId: string;
  payloadVersion: number;
  payload: JsonObject;
  claimToken: string;
  attemptCount: number;
  createdAt: string;
}

export interface OutboxClaimRenewalStore {
  renew(event: OutboxEvent, leaseSeconds: number): Promise<boolean>;
}

export interface OutboxStore extends OutboxClaimRenewalStore {
  claim(topics: readonly string[], limit: number, leaseSeconds: number): Promise<OutboxEvent[]>;
  complete(event: OutboxEvent): Promise<boolean>;
  fail(
    event: OutboxEvent,
    error: string,
    retryDelaySeconds: number,
    attemptLimit: number,
  ): Promise<boolean>;
}

export interface OutboxStats {
  pendingCount: number;
  processingCount: number;
  deliveredCount: number;
  deadLetterCount: number;
  oldestPendingAt: string | null;
}

export interface OutboxDeadLetterCursor {
  createdAt: string;
  eventId: string;
}

export interface OutboxDeadLetter {
  eventId: string;
  tenantId: string;
  topic: string;
  aggregateType: string;
  aggregateId: string;
  payloadVersion: number;
  attemptCount: number;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface OutboxDeadLetterListOptions {
  limit?: number;
  cursor?: OutboxDeadLetterCursor | null;
}

export interface OutboxDeadLetterPage {
  items: OutboxDeadLetter[];
  nextCursor: OutboxDeadLetterCursor | null;
}

export interface OutboxRecoveryStore extends OutboxStore {
  stats(options?: { limit?: number }): Promise<OutboxStats>;
  listDeadLetters(options?: OutboxDeadLetterListOptions): Promise<OutboxDeadLetterPage>;
  requeue(eventId: string): Promise<boolean>;
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
  measures: JsonObject;
  dimensions: JsonObject;
  metadata: JsonObject;
  requested: string;
  charged: string;
  allowanceRequested: string;
  allowanceCovered: string;
  billingDisposition: "billable" | "record_only";
  catalogRevisionId: string | null;
  planId: string | null;
  rateCardKey: string | null;
  pricingSnapshot: JsonObject;
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
  envelope: JsonObject | null;
  objectKey: string | null;
  objectVersion: string | null;
  archivedAt: string | null;
}

export interface UsageEventSink {
  initialize?(): Promise<void>;
  /** Non-mutating compatibility check for a caller-managed projection schema. */
  checkSchemaCompatibility?(): Promise<void>;
  writeUsage(event: UsageChargeExport, outboxEventId: string): Promise<void>;
  writeUsageBatch?(entries: readonly (readonly [UsageChargeExport, string])[]): Promise<void>;
}

export interface BillingPayloadArchiveResult {
  key: string;
  versionId: string | null;
}

export interface BillingPayloadArchive {
  archive(event: BillingEventPayloadExport): Promise<BillingPayloadArchiveResult>;
  close?(): Promise<void> | void;
}
