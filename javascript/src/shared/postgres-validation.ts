import { z } from "zod";
import { StoreError } from "../errors.js";

/** PostgreSQL accepts any 128-bit UUID value, including non-RFC version bits. */
export const postgresUuid = z
  .string()
  .regex(/^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$/);

/** PostgreSQL drivers return boolean columns as booleans; reject transport-shaped coercions. */
export const pgBoolean = z.boolean();

/** Unwrap a single-key JSONB result row. Matches Python _unwrap_jsonb behavior. */
export function unwrapJsonb(rows: unknown[]): Record<string, unknown> | null {
  if (rows.length !== 1) return null;
  const row = rows[0];
  if (row === null || typeof row !== "object" || Array.isArray(row)) return null;
  const r = row as Record<string, unknown>;
  const keys = Object.keys(r);
  const key = keys[0];
  if (keys.length === 1 && key !== undefined) {
    const v = r[key];
    if (v === null) return null;
    if (typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  }
  return r;
}

/** Parse a Zod schema, converting validation errors to StoreError. */
export function safeParse<T>(
  schema: z.ZodType<T>,
  data: unknown,
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
export function requireRow(rows: readonly unknown[] | null | undefined, context: string): unknown {
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
  rows: readonly unknown[] | null | undefined,
  context: string,
): Record<string, unknown> {
  const row = requireRow(rows, context);
  if (typeof row !== "object" || row === null || Array.isArray(row)) {
    throw new StoreError(`${context}: expected an object result`, {
      details: { context },
      indeterminate: true,
    });
  }
  return row as Record<string, unknown>;
}

/** Return an optional singleton query row and reject ambiguous/malformed results. */
export function optionalRecordRow(
  rows: readonly unknown[] | null | undefined,
  context: string,
): Record<string, unknown> | null {
  if (!rows?.length) return null;
  if (rows.length !== 1) {
    throw new StoreError(`${context}: expected at most one result row, received ${rows.length}`, {
      details: { context, rowCount: rows.length },
    });
  }
  const row = rows[0];
  if (typeof row !== "object" || row === null || Array.isArray(row)) {
    throw new StoreError(`${context}: expected an object result`, {
      details: { context },
    });
  }
  return row as Record<string, unknown>;
}

/** Require and validate one named field from a mutation result row. */
export function requireResultField<T>(
  rows: readonly unknown[] | null | undefined,
  key: string,
  schema: z.ZodType<T>,
  context: string,
): T {
  const row = requireRecordRow(rows, context);
  return safeParse(schema, row[key], `${context}.${key}`, { indeterminate: true });
}
