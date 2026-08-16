import type { Decimal } from "decimal.js";
import { z } from "zod";

/** JSON values accepted at configuration and persistence boundaries. */
export type JsonPrimitive = string | number | boolean | null;

export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];

export interface JsonObject {
  [key: string]: JsonValue;
}

/** Values accepted from provider SDK envelopes before field-level validation. */
export type ExternalValue = JsonPrimitive | undefined | Date | ExternalObject | ExternalValue[];

export interface ExternalObject {
  [key: string]: ExternalValue;
}

const externalValueSchema: z.ZodType<ExternalValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number(),
    z.boolean(),
    z.null(),
    z.undefined(),
    z.date(),
    z.array(externalValueSchema),
    z.record(z.string(), externalValueSchema),
  ]),
);
const externalObjectSchema: z.ZodType<ExternalObject> = z.record(z.string(), externalValueSchema);
const jsonObjectSchema: z.ZodType<JsonObject> = z.record(z.string(), z.json());

/** Structured application metadata may retain exact Decimal instances. */
export type StructuredValue =
  | JsonPrimitive
  | bigint
  | undefined
  | Decimal
  | StructuredObject
  | StructuredValue[];

export interface StructuredObject {
  [key: string]: StructuredValue;
}

/** Values the PostgreSQL driver can encode or return without stringification. */
export type PostgresValue = StructuredValue | PostgresRow | Date | Uint8Array;

export type PostgresRow = {
  [key: string]: PostgresValue;
};

export type PostgresParams = (PostgresValue | undefined)[];

export function isPostgresRow(value: PostgresValue): value is PostgresRow {
  try {
    return (
      value !== null &&
      !Array.isArray(value) &&
      Object.prototype.toString.call(value) === "[object Object]"
    );
  } catch {
    return false;
  }
}

/** Check the object shape produced by JSON/YAML decoding without coercion. */
export function isJsonObject<T>(value: T): value is T & JsonObject {
  try {
    return jsonObjectSchema.safeParse(value).success;
  } catch {
    return false;
  }
}

export function isExternalObject<T>(value: T): value is T & ExternalObject {
  try {
    return externalObjectSchema.safeParse(value).success;
  } catch {
    return false;
  }
}
