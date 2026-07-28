import Decimal from "decimal.js";

import type { AllowancePeriod } from "../../allowance.js";
import { StoreError } from "../../errors.js";
import type { LedgerEntryRow } from "./repositories/analytics.js";
import type {
  BillingMode,
  BursarConfigResult,
  LedgerEntry,
  OperationPolicy,
} from "../types/index.js";

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

export function compatibilityOperationPolicies(
  operations: Record<string, { maxInFlight: number | null }>,
  billingMode: BillingMode,
  overdraftFloor: Decimal | null,
): Record<string, OperationPolicy> {
  return Object.fromEntries(
    Object.entries(operations).map(([operation, policy]) => [
      operation,
      {
        billingMode,
        maxConcurrent: policy.maxInFlight,
        overdraftFloor,
      },
    ]),
  );
}

export function compatibilityAllowancePeriod(row: Record<string, unknown>): AllowancePeriod | null {
  const unit = row.credit_allowance_reset_unit;
  const count = Number(row.credit_allowance_reset_count ?? 0);
  const anchor = row.credit_allowance_reset_anchor;
  if (anchor === "calendar" && unit === "month" && count === 1) {
    return "calendar_month";
  }
  if (anchor === "rolling" && unit === "day" && count === 30) {
    return "rolling_30d";
  }
  if (anchor === "plan_assignment" && unit === "month" && count === 1) {
    return "anniversary";
  }
  return null;
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
    referenceEntryId: row.reference_entry_id == null ? null : String(row.reference_entry_id),
    idempotencyKey: row.idempotency_key == null ? null : String(row.idempotency_key),
    metadata: row.metadata ?? null,
    createdAt:
      row.created_at instanceof Date ? row.created_at.toISOString() : String(row.created_at),
  };
}
