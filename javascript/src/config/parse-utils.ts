import { Decimal } from "decimal.js";
import { z } from "zod";

import { ConfigError } from "../errors.js";
import { isJsonObject, type JsonObject, type JsonValue } from "../shared/json.js";
import type { Availability, BillingInterval, Duration, ExpiryPolicy, Window } from "./types.js";

const IDENTIFIER = /^[a-z][a-z0-9_]*$/;
const REGION = /^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$/;

export type { JsonObject, JsonValue } from "../shared/json.js";

type ConfigValue = JsonValue | undefined;

const DURATION_UNITS = ["second", "minute", "hour", "day", "week"] as const;
const BILLING_INTERVAL_UNITS = ["day", "week", "month", "year"] as const;
const CALENDAR_UNITS = ["day", "week", "month", "year"] as const;

function requiredValue(value: ConfigValue, path: string): JsonValue {
  if (value === undefined) semanticError(`${path} is required`);
  return value;
}

export function asObject(value: ConfigValue, path = "value"): JsonObject {
  const candidate = requiredValue(value, path);
  if (!isJsonObject(candidate)) semanticError(`${path} must be an object`);
  return candidate;
}

export function asArray(value: ConfigValue, path = "value"): JsonValue[] {
  const candidate = requiredValue(value, path);
  if (!Array.isArray(candidate)) semanticError(`${path} must be an array`);
  return candidate;
}

export function asStringArray(value: ConfigValue, path = "value"): string[] {
  return asArray(value, path).map((item, index) => asString(item, `${path}[${index}]`));
}

export function semanticError(message: string): never {
  throw new ConfigError(message);
}

export function identifier(value: string, path: string): string {
  if (!IDENTIFIER.test(value)) semanticError(`${path} must be a non-empty snake_case identifier`);
  return value;
}

export function validateIdentifiers(value: JsonObject, path: string): void {
  for (const key of Object.keys(value)) identifier(key, `${path}.${key}`);
}

export function validateRegions(values: string[], path: string): void {
  if (new Set(values).size !== values.length) {
    semanticError(`${path} must not contain duplicates`);
  }
  if (values.some((value) => !REGION.test(value))) {
    semanticError(`${path} must contain uppercase ISO-style region codes`);
  }
}

export function asDecimal(value: ConfigValue, path = "value"): Decimal {
  const candidate = requiredValue(value, path);
  const parsed = z.union([z.string(), z.number()]).safeParse(candidate);
  if (!parsed.success) {
    semanticError(`${path} must be a decimal string or number`);
  }
  try {
    return new Decimal(parsed.data);
  } catch {
    semanticError(`${path} must be a valid decimal`);
  }
}

export function asInteger(value: ConfigValue, path = "value"): number {
  const candidate = requiredValue(value, path);
  const parsed = z.number().int().safeParse(candidate);
  if (!parsed.success) {
    semanticError(`${path} must be an integer`);
  }
  return parsed.data;
}

export function asString(value: ConfigValue, path = "value"): string {
  const candidate = requiredValue(value, path);
  const parsed = z.string().safeParse(candidate);
  if (!parsed.success) semanticError(`${path} must be a string`);
  return parsed.data;
}

export function asBoolean(value: ConfigValue, path = "value"): boolean {
  const candidate = requiredValue(value, path);
  const parsed = z.boolean().safeParse(candidate);
  if (!parsed.success) semanticError(`${path} must be a boolean`);
  return parsed.data;
}

function enumValue<const T extends string>(
  value: ConfigValue,
  allowed: readonly T[],
  path: string,
): T {
  const candidate = asString(value, path);
  const match = allowed.find((item) => item === candidate);
  if (match === undefined) semanticError(`${path} has an invalid value`);
  return match;
}

function asTimezone(value: ConfigValue, path: string): string {
  const candidate = asString(value, path);
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: candidate }).format();
  } catch {
    semanticError(`${path} must be a valid IANA timezone`);
  }
  return candidate;
}

export function parseDuration(value: ConfigValue): Duration {
  const raw = asObject(value, "duration");
  return {
    unit: enumValue(raw.unit, DURATION_UNITS, "duration.unit"),
    count: asInteger(raw.count, "duration.count"),
  };
}

export function parseBillingInterval(value: ConfigValue): BillingInterval {
  const raw = asObject(value, "billing_interval");
  return {
    unit: enumValue(raw.unit, BILLING_INTERVAL_UNITS, "billing_interval.unit"),
    count: asInteger(raw.count ?? 1, "billing_interval.count"),
  };
}

export function parseWindow(value: ConfigValue, path: string): Window {
  const raw = asObject(value);
  if (raw.type === "rolling") {
    return { type: "rolling", duration: parseDuration(raw.duration) };
  }
  if (raw.type === "plan_assignment") {
    return {
      type: "plan_assignment",
      interval: parseBillingInterval(raw.interval),
      timezone: asTimezone(raw.timezone ?? "UTC", `${path}.timezone`),
    };
  }
  if (raw.type !== "calendar") semanticError(`${path}.type must be a supported window type`);
  return {
    type: "calendar",
    unit: enumValue(raw.unit, CALENDAR_UNITS, `${path}.unit`),
    count: asInteger(raw.count ?? 1, `${path}.count`),
    timezone: asTimezone(raw.timezone ?? "UTC", `${path}.timezone`),
  };
}

export function parseAvailability(value: ConfigValue): Availability {
  const raw = asObject(value, "availability");
  const startsAt =
    raw.starts_at == null ? undefined : asString(raw.starts_at, "availability.starts_at");
  const endsAt = raw.ends_at == null ? undefined : asString(raw.ends_at, "availability.ends_at");
  const regions = Array.isArray(raw.regions)
    ? raw.regions.map((region, index) => asString(region, `availability.regions[${index}]`))
    : [];
  if (raw.regions != null && !Array.isArray(raw.regions)) {
    semanticError("availability.regions must be an array");
  }
  validateRegions(regions, "availability.regions");
  if (startsAt != null && endsAt != null && Date.parse(endsAt) <= Date.parse(startsAt)) {
    semanticError("availability.ends_at must be later than starts_at");
  }
  const result: Availability = { regions };
  if (startsAt != null) result.startsAt = startsAt;
  if (endsAt != null) result.endsAt = endsAt;
  return result;
}

export function parseExpiry(value: ConfigValue, path: string): ExpiryPolicy {
  const raw = asObject(value);
  switch (raw.type) {
    case "after_grant":
      return {
        type: "after_grant",
        interval: parseBillingInterval(raw.interval),
        timezone: asTimezone(raw.timezone ?? "UTC", `${path}.timezone`),
      };
    case "end_of_window": {
      const parsed = parseWindow(raw.window, `${path}.window`);
      if (parsed.type === "rolling") semanticError(`${path} cannot use a rolling window`);
      return { type: "end_of_window", window: parsed };
    }
    case "fixed_at":
      return { type: "fixed_at", at: asString(raw.at, `${path}.at`) };
    case "subscription_end":
      return { type: "subscription_end" };
    default:
      return { type: "never" };
  }
}
