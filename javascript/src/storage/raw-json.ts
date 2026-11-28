import { z } from "zod";
import type { PostgresValue } from "../shared/json.js";

/** Exact SQL-projected JSON fragments, kept alongside ordinary consumer values. */
export interface RawUsageJson {
  measures?: string;
  dimensions?: string;
  metadata?: string;
  pricing_snapshot?: string;
}

const rawUsageSchema = z.object({
  measures: z.string().nullable(),
  dimensions: z.string().nullable(),
  metadata: z.string().nullable(),
  pricing_snapshot: z.string().nullable(),
});

export function rawUsageJson(value: PostgresValue | undefined): RawUsageJson | undefined {
  const parsed = rawUsageSchema.safeParse(value);
  if (!parsed.success) return undefined;
  const result: RawUsageJson = {};
  for (const key of ["measures", "dimensions", "metadata", "pricing_snapshot"] as const) {
    if (parsed.data[key] !== null) result[key] = parsed.data[key];
  }
  return result;
}
