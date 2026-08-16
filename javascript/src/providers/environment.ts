import { z } from "zod";

/** Financial provider namespace used by both persistence and provider adapters. */
export const PROVIDER_ENVIRONMENTS = ["live", "test", "sandbox"] as const;

export type ProviderEnvironment = (typeof PROVIDER_ENVIRONMENTS)[number];

/** Validate a caller-supplied provider namespace without coercing unsafe aliases. */
export function normalizeProviderEnvironment<T>(value: T): ProviderEnvironment {
  const parsed = z.enum(PROVIDER_ENVIRONMENTS).safeParse(value);
  if (!parsed.success) {
    throw new TypeError("providerEnvironment must be 'live', 'test', or 'sandbox'");
  }
  return parsed.data;
}
