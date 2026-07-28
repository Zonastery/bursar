import type { Decimal } from "decimal.js";

// ── Canonical ledger history ─────────────────────────────────────────

/** One immutable monetary entry on a canonical credit account. */
export interface LedgerEntry {
  entryId: string;
  accountId: string;
  actorUserId: string | null;
  amount: Decimal;
  entryType: string;
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

/** Cursor-only options for the usage-only ledger view. */
export interface ListUsageEntriesOptions {
  fromDate?: Date;
  toDate?: Date;
  limit?: number;
  cursor?: LedgerCursor | null;
}
