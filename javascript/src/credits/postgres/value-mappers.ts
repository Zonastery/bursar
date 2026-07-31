import Decimal from "decimal.js";

import { StoreError } from "../../errors.js";
import type { LedgerEntryRow, UsageChargeRow } from "./repositories/analytics.js";
import type { BursarConfigResult, LedgerEntry, UsageCharge } from "../types/index.js";

export const ZERO = new Decimal(0);

export function decimalValue(value: unknown, fallback: Decimal = ZERO): Decimal {
  if (value === null || value === undefined) return fallback;
  if (value instanceof Decimal) return value;
  try {
    return new Decimal(typeof value === "string" ? value : String(value));
  } catch {
    throw new StoreError(`Failed to parse Decimal value: ${String(value)}`);
  }
}

export function decimalParameter(value: Decimal): string {
  return value.toString();
}

export function decimalRecord(raw: unknown): Record<string, Decimal> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).map(([key, value]) => [
      key,
      decimalValue(value),
    ]),
  );
}

export function parseEntitlements(raw: unknown): Record<string, { value: unknown }> {
  if (!raw || typeof raw !== "object") return {};
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).map(([key, value]) => {
      const record = value as Record<string, unknown> | null;
      return [
        key,
        {
          value:
            record &&
            typeof record === "object" &&
            Object.prototype.hasOwnProperty.call(record, "value")
              ? record.value
              : value,
        },
      ];
    }),
  );
}

export function parseAdmissionOperations(
  raw: unknown,
): Record<string, { maxInFlight: number | null }> {
  if (!raw || typeof raw !== "object") return {};
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).map(([key, value]) => {
      const operation = (value ?? {}) as Record<string, unknown>;
      return [
        key,
        {
          maxInFlight: operation.max_in_flight == null ? null : Number(operation.max_in_flight),
        },
      ];
    }),
  );
}

export function normalizeBursarConfig(
  row: Record<string, unknown>,
  defaultVersion: number,
): BursarConfigResult {
  const config = row.config as Record<string, unknown> | undefined;
  return {
    id: String(row.id ?? ""),
    config: config ?? {},
    version: config == null ? defaultVersion : Number(row.version ?? defaultVersion),
  };
}

export function mapLedgerEntry(row: LedgerEntryRow): LedgerEntry {
  return {
    entryId: String(row.entry_id),
    accountId: String(row.account_id),
    actorUserId: row.actor_user_id == null ? null : String(row.actor_user_id),
    amount: decimalValue(row.amount),
    entryType: String(row.entry_type),
    operation: String(row.operation),
    referenceEntryId: row.reference_entry_id == null ? null : String(row.reference_entry_id),
    idempotencyKey: row.idempotency_key == null ? null : String(row.idempotency_key),
    metadata: row.metadata ?? null,
    createdAt:
      row.created_at instanceof Date ? row.created_at.toISOString() : String(row.created_at),
  };
}

export function mapUsageCharge(row: UsageChargeRow): UsageCharge {
  const toIso = (value: string | Date): string =>
    value instanceof Date ? value.toISOString() : String(value);
  return {
    usageId: String(row.usage_id),
    accountId: String(row.account_id),
    operation: String(row.operation),
    requested: decimalValue(row.requested),
    charged: decimalValue(row.charged),
    allowanceRequested: decimalValue(row.allowance_requested),
    allowanceCovered: decimalValue(row.allowance_covered),
    feature: row.feature == null ? null : String(row.feature),
    model: row.model == null ? null : String(row.model),
    region: row.region == null ? null : String(row.region),
    eventAt: toIso(row.event_at),
    idempotencyKey: String(row.idempotency_key),
    metadata: row.metadata ?? null,
    createdAt: toIso(row.created_at),
  };
}
