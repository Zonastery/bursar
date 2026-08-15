/** Package-scoped telemetry constants shared by every instrumentation adapter. */
export const BURSAR_INSTRUMENTATION_SCOPE = "@zonastery/bursar";
export const BURSAR_INSTRUMENTATION_VERSION = "2.0.3";

export type TelemetryAttributeValue = string | number | boolean;
export type TelemetryAttributes = Readonly<Record<string, TelemetryAttributeValue>>;

const ALLOWED_ATTRIBUTE_KEYS = new Set([
  "bursar.operation",
  "bursar.outcome",
  "bursar.backend",
  "bursar.provider",
  "error.type",
  "error.code",
]);
const MAX_ATTRIBUTE_LENGTH = 64;

function normalizeToken(value: unknown, fallback?: string): string | undefined {
  if (typeof value !== "string") return fallback;
  const normalized = value
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^[_\-.]+|[_\-.]+$/g, "")
    .toLowerCase()
    .slice(0, MAX_ATTRIBUTE_LENGTH);
  return normalized || fallback;
}

/**
 * Keep telemetry attributes bounded and low-cardinality.
 *
 * Unknown keys and non-scalar values are discarded. String values are
 * normalized to stable tokens so identifiers, metadata, and raw payloads
 * cannot accidentally pass through this boundary.
 */
export function sanitizeTelemetryAttributes(
  attributes: Readonly<Record<string, unknown>> = {},
): Record<string, TelemetryAttributeValue> {
  const sanitized: Record<string, TelemetryAttributeValue> = {};
  for (const [key, value] of Object.entries(attributes)) {
    if (!ALLOWED_ATTRIBUTE_KEYS.has(key)) continue;
    if (typeof value === "boolean") {
      sanitized[key] = value;
      continue;
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      sanitized[key] = value;
      continue;
    }
    const normalized = normalizeToken(value);
    if (normalized !== undefined) sanitized[key] = normalized;
  }
  return sanitized;
}

/** Build the safe base attributes for one Bursar operation. */
export function telemetryOperationAttributes(
  operation: string,
  attributes: Readonly<Record<string, unknown>> = {},
): Record<string, TelemetryAttributeValue> {
  return {
    ...sanitizeTelemetryAttributes(attributes),
    "bursar.operation": normalizeToken(operation, "unknown")!,
  };
}

/** Normalize an error without reading or recording its message or details. */
export function telemetryErrorAttributes(error: unknown): Record<string, string> {
  const type = normalizeToken(diagnosticErrorType(error), "unknown_error")!;
  const code = normalizeToken(diagnosticErrorCode(error));
  return {
    "error.type": type,
    ...(code === undefined ? {} : { "error.code": code }),
  };
}

/** Vendor-neutral contract used by Bursar's core runtime. */
export interface Instrumentation {
  run<T>(
    operation: string,
    attributes: TelemetryAttributes | undefined,
    callback: () => Promise<T>,
  ): Promise<T>;
}

/** Default implementation: execute normally and emit nothing. */
export class NoopInstrumentation implements Instrumentation {
  async run<T>(
    _operation: string,
    _attributes: TelemetryAttributes | undefined,
    callback: () => Promise<T>,
  ): Promise<T> {
    return callback();
  }
}

export const NOOP_INSTRUMENTATION: Instrumentation = Object.freeze(new NoopInstrumentation());

let defaultInstrumentation: Instrumentation = NOOP_INSTRUMENTATION;
const instrumentationRegistrations: Array<{
  instrumentation: Instrumentation;
  active: boolean;
}> = [];

function assertInstrumentation(value: Instrumentation): void {
  if (typeof value !== "object" || value === null || typeof value.run !== "function") {
    throw new TypeError("instrumentation must provide run()");
  }
}

/** Return the instrumentation selected by the embedding application. */
export function getDefaultInstrumentation(): Instrumentation {
  return defaultInstrumentation;
}

/**
 * Select a process-wide Bursar instrumentation implementation.
 *
 * This does not configure an OpenTelemetry provider or exporter. Call the
 * returned function to restore the previous implementation. A stale restore
 * cannot overwrite a newer registration.
 */
export function setDefaultInstrumentation(instrumentation: Instrumentation | null): () => void {
  const selected = instrumentation ?? NOOP_INSTRUMENTATION;
  assertInstrumentation(selected);
  const registration = { instrumentation: selected, active: true };
  instrumentationRegistrations.push(registration);
  refreshDefaultInstrumentation();
  let restored = false;
  return () => {
    if (restored) return;
    restored = true;
    registration.active = false;
    refreshDefaultInstrumentation();
  };
}

function refreshDefaultInstrumentation(): void {
  while (instrumentationRegistrations.at(-1)?.active === false) {
    instrumentationRegistrations.pop();
  }
  defaultInstrumentation =
    instrumentationRegistrations.at(-1)?.instrumentation ?? NOOP_INSTRUMENTATION;
}
import { diagnosticErrorCode, diagnosticErrorType } from "../shared/diagnostics.js";
