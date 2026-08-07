import type { Decimal } from "decimal.js";

// ── Canonical ledger history ─────────────────────────────────────────

/** One immutable monetary entry on a canonical credit account. */
export interface LedgerEntry {
  entryId: string;
  accountId: string;
  actorUserId: string | null;
  amount: Decimal;
  entryType: string;
  operation: string;
  referenceEntryId: string | null;
  idempotencyKey: string | null;
  metadata: Record<string, unknown> | null;
  createdAt: string;
}

/** Stable position in an account ledger. */
export interface LedgerCursor {
  createdAt: string;
  entryId: string;
}

/** Cursor-only options for listing account ledger entries. */
export interface ListLedgerEntriesOptions {
  entryTypes?: string[];
  fromDate?: Date;
  toDate?: Date;
  limit?: number;
  cursor?: LedgerCursor | null;
}

/** One stable cursor page. */
export interface LedgerPage {
  items: LedgerEntry[];
  nextCursor: LedgerCursor | null;
}

/**
 * A metered usage charge from the usage charge journal.
 *
 * Usage charges are deliberately separate from ledger entries: included
 * allowance consumption does not create a monetary ledger debit, but it is
 * still a billable usage event that belongs in the usage history.
 */
export interface UsageCharge {
  usageId: string;
  accountId: string;
  operation: string;
  requested: Decimal;
  charged: Decimal;
  allowanceRequested: Decimal;
  allowanceCovered: Decimal;
  billingDisposition: "billable" | "record_only";
  feature: string | null;
  model: string | null;
  region: string | null;
  eventAt: string;
  idempotencyKey: string;
  metadata: Record<string, unknown> | null;
  createdAt: string;
}

export interface UsageChargeCursor {
  eventAt: string;
  usageId: string;
}

export interface ListUsageChargesOptions {
  fromDate?: Date;
  toDate?: Date;
  limit?: number;
  cursor?: UsageChargeCursor | null;
  includeRecordOnly?: boolean;
}

export interface UsageChargePage {
  items: UsageCharge[];
  nextCursor: UsageChargeCursor | null;
}

/** Result of appending a usage event without creating another balance debit. */
interface UsageRecordResultBase {
  userId: string;
  requested: Decimal;
  idempotent: boolean;
}

export interface UsageRecordSuccess extends UsageRecordResultBase {
  error: null;
  usageId: string;
}

export interface UsageRecordFailure extends UsageRecordResultBase {
  error: string;
  usageId: null;
  idempotent: false;
}

export type UsageRecordResult = UsageRecordSuccess | UsageRecordFailure;

/** Cursor-only options for the usage-only ledger view. */
export interface ListUsageEntriesOptions {
  fromDate?: Date;
  toDate?: Date;
  limit?: number;
  cursor?: LedgerCursor | null;
}
