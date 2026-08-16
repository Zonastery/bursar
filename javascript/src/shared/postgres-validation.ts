import { z } from "zod";
import { StoreError } from "../errors.js";
import {
  isJsonObject,
  isPostgresRow,
  type JsonObject,
  type PostgresRow,
  type PostgresValue,
} from "./json.js";

/** PostgreSQL accepts any 128-bit UUID value, including non-RFC version bits. */
export const postgresUuid = z
  .string()
  .regex(/^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$/);

/** PostgreSQL drivers return boolean columns as booleans; reject transport-shaped coercions. */
export const pgBoolean = z.boolean();

/** Unwrap a single-key JSONB result row. Matches Python _unwrap_jsonb behavior. */
export function unwrapJsonb(rows: PostgresRow[]): JsonObject | null {
  if (rows.length !== 1) return null;
  const row = rows[0];
  if (row === undefined) return null;
  const keys = Object.keys(row);
  const key = keys[0];
  if (keys.length === 1 && key !== undefined) {
    const v = row[key];
    if (v === null || v === undefined) return null;
    if (isJsonObject(v)) return v;
  }
  return isJsonObject(row) ? row : null;
}

/** Parse a Zod schema, converting validation errors to StoreError. */
export function safeParse<T>(
  schema: z.ZodType<T>,
  data: z.input<z.ZodType<T>>,
  context: string,
  options: { indeterminate?: boolean } = {},
): T {
  try {
    return schema.parse(data);
  } catch (e) {
    throw new StoreError(
      `${context}: schema validation failed — ${e instanceof Error ? e.message : String(e)}`,
      {
        cause: e,
        details: { context },
        indeterminate: options.indeterminate ?? false,
      },
    );
  }
}

/** Require the single-row envelope promised by a scalar or mutation RPC. */
export function requireRow(
  rows: readonly PostgresValue[] | null | undefined,
  context: string,
): PostgresValue {
  if (rows?.length !== 1 || rows[0] == null) {
    throw new StoreError(
      `${context}: expected exactly one result row, received ${rows?.length ?? 0}`,
      {
        details: { context, rowCount: rows?.length ?? 0 },
        indeterminate: true,
      },
    );
  }
  return rows[0];
}

/** Require a single object result from a scalar or mutation RPC. */
export function requireRecordRow(
  rows: readonly PostgresValue[] | null | undefined,
  context: string,
): PostgresRow {
  const row = requireRow(rows, context);
  if (!isPostgresRow(row)) {
    throw new StoreError(`${context}: expected an object result`, {
      details: { context },
      indeterminate: true,
    });
  }
  return row;
}

/** Return an optional singleton query row and reject ambiguous/malformed results. */
export function optionalRecordRow(
  rows: readonly PostgresValue[] | null | undefined,
  context: string,
): PostgresRow | null {
  if (!rows?.length) return null;
  if (rows.length !== 1) {
    throw new StoreError(`${context}: expected at most one result row, received ${rows.length}`, {
      details: { context, rowCount: rows.length },
    });
  }
  const row = rows[0];
  if (row === undefined || !isPostgresRow(row)) {
    throw new StoreError(`${context}: expected an object result`, {
      details: { context },
    });
  }
  return row;
}

/** Require and validate one named field from a mutation result row. */
export function requireResultField<T>(
  rows: readonly PostgresValue[] | null | undefined,
  key: string,
  schema: z.ZodType<T>,
  context: string,
): T {
  const row = requireRecordRow(rows, context);
  const value = row[key];
  if (value === undefined) {
    throw new StoreError(`${context}.${key}: result field is missing`, {
      details: { context, key },
      indeterminate: true,
    });
  }
  return safeParse(schema, value, `${context}.${key}`, { indeterminate: true });
}
