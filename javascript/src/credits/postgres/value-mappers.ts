import { Decimal } from "decimal.js";

import { StoreError } from "../../errors.js";
import { isJsonObject, type JsonObject, type PostgresValue } from "../../shared/json.js";
import type { LedgerEntryRow, UsageChargeRow } from "./repositories/analytics.js";
import type { CatalogRevision, LedgerEntry, UsageCharge } from "../types/index.js";

export const ZERO = new Decimal(0);

export function decimalValue(value: PostgresValue | undefined): Decimal {
  if (
    value === null ||
    value === undefined ||
    value instanceof Date ||
    value instanceof Uint8Array
  ) {
    throw new StoreError("PostgreSQL returned a missing or invalid Decimal value", {
      details: { valueType: "non-scalar" },
    });
  }
  try {
    const parsed = value instanceof Decimal ? value : new Decimal(String(value));
    if (!parsed.isFinite()) {
      throw new Error("Decimal value must be finite");
    }
    return parsed;
  } catch (cause) {
    throw new StoreError(`Failed to parse Decimal value: ${String(value)}`, {
      cause,
      details: { valueType: "invalid" },
    });
  }
}

export function decimalParameter(value: Decimal): string {
  return value.toString();
}

export function decimalRecord(raw: PostgresValue | undefined): Record<string, Decimal> | null {
  if (raw === null || raw === undefined || !isJsonObject(raw)) return null;
  return Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, decimalValue(value)]));
}

export function normalizeCatalogRevision(row: {
  id: string;
  config: JsonObject;
  version: number;
}): CatalogRevision {
  return {
    id: row.id,
    config: row.config,
    version: row.version,
  };
}

export function mapLedgerEntry(row: LedgerEntryRow): LedgerEntry {
  return {
    entryId: row.entry_id,
    accountId: row.account_id,
    actorUserId: row.actor_user_id,
    amount: decimalValue(row.amount),
    entryType: row.entry_type,
    operation: row.operation,
    referenceEntryId: row.reference_entry_id,
    idempotencyKey: row.idempotency_key,
    metadata: row.metadata,
    createdAt: row.created_at,
  };
}

export function mapUsageCharge(row: UsageChargeRow): UsageCharge {
  return {
    usageId: row.usage_id,
    accountId: row.account_id,
    operation: row.operation,
    requested: decimalValue(row.requested),
    charged: decimalValue(row.charged),
    allowanceRequested: decimalValue(row.allowance_requested),
    allowanceCovered: decimalValue(row.allowance_covered),
    billingDisposition: row.billing_disposition,
    feature: row.feature,
    model: row.model,
    region: row.region,
    eventAt: row.event_at,
    idempotencyKey: row.idempotency_key,
    metadata: row.metadata,
    createdAt: row.created_at,
  };
}
