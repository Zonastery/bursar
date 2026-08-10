/** Financial provider namespace used by both persistence and provider adapters. */
export const PROVIDER_ENVIRONMENTS = ["live", "test", "sandbox"] as const;

export type ProviderEnvironment = (typeof PROVIDER_ENVIRONMENTS)[number];

/** Validate a caller-supplied provider namespace without coercing unsafe aliases. */
export function normalizeProviderEnvironment(value: unknown): ProviderEnvironment {
  if (value !== "live" && value !== "test" && value !== "sandbox") {
    throw new TypeError("providerEnvironment must be 'live', 'test', or 'sandbox'");
  }
  return value;
}
