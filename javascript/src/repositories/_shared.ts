import { z } from "zod";
import { StoreError } from "../errors.js";

/** Parse pg boolean values that may arrive as string/boolean/number. */
export const pgBoolean = z.union([z.boolean(), z.string(), z.number()]).transform((v) => {
  if (typeof v === "boolean") return v;
  if (typeof v === "string") return v === "true" || v === "t" || v === "1";
  if (typeof v === "number") return v !== 0;
  return false;
});

/** Unwrap a single-key JSONB result row. Matches Python _unwrap_jsonb behavior. */
export function unwrapJsonb(rows: unknown[]): Record<string, unknown> | null {
  if (rows.length !== 1) return null;
  const row = rows[0];
  if (row === null || typeof row !== "object" || Array.isArray(row)) return null;
  const r = row as Record<string, unknown>;
  const keys = Object.keys(r);
  if (keys.length === 1) {
    const v = r[keys[0]];
    if (v === null) return null;
    if (typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  }
  return r;
}

/** Parse a Zod schema, converting validation errors to StoreError. */
export function safeParse<T>(schema: z.ZodType<T>, data: unknown, context: string): T {
  try {
    return schema.parse(data);
  } catch (e) {
    throw new StoreError(
      `${context}: schema validation failed — ${e instanceof Error ? e.message : String(e)}`,
    );
  }
}
