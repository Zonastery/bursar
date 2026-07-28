import Decimal from "decimal.js";

import { ConfigError } from "../errors.js";
import type { Availability, BillingInterval, Duration, ExpiryPolicy, Window } from "./types.js";

const IDENTIFIER = /^[a-z][a-z0-9_]*$/;

export type JsonObject = Record<string, unknown>;

export function asObject(value: unknown): JsonObject {
  return value as JsonObject;
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

export function asDecimal(value: unknown): Decimal {
  return new Decimal(value as Decimal.Value);
}

export function asInteger(value: unknown): number {
  return Number(value);
}

export function asString(value: unknown): string {
  return String(value);
}

export function asBoolean(value: unknown): boolean {
  return Boolean(value);
}

function asTimezone(value: unknown, path: string): string {
  const candidate = asString(value);
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: candidate }).format();
  } catch {
    semanticError(`${path} must be a valid IANA timezone`);
  }
  return candidate;
}

export function parseDuration(value: unknown): Duration {
  const raw = asObject(value);
  return { unit: raw.unit as Duration["unit"], count: asInteger(raw.count) };
}

export function parseBillingInterval(value: unknown): BillingInterval {
  const raw = asObject(value);
  return {
    unit: raw.unit as BillingInterval["unit"],
    count: asInteger(raw.count ?? 1),
  };
}

export function parseWindow(value: unknown, path: string): Window {
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
  return {
    type: "calendar",
    unit: raw.unit as Extract<Window, { type: "calendar" }>["unit"],
    count: asInteger(raw.count ?? 1),
    timezone: asTimezone(raw.timezone ?? "UTC", `${path}.timezone`),
  };
}

export function parseAvailability(value: unknown): Availability {
  const raw = asObject(value);
  return {
    ...(raw.starts_at == null ? {} : { startsAt: asString(raw.starts_at) }),
    ...(raw.ends_at == null ? {} : { endsAt: asString(raw.ends_at) }),
    regions: (raw.regions ?? []) as string[],
  };
}

export function parseExpiry(value: unknown, path: string): ExpiryPolicy {
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
      return { type: "fixed_at", at: asString(raw.at) };
    case "subscription_end":
      return { type: "subscription_end" };
    default:
      return { type: "never" };
  }
}
