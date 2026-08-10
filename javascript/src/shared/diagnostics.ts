/** Maximum diagnostic length accepted by the SQL layer. */
export const PERSISTED_DIAGNOSTIC_MAX_CHARACTERS = 8_192;

const DIAGNOSTIC_CODE_MAX_CHARACTERS = 128;

/** Return trimmed, non-empty text accepted by diagnostic SQL constraints. */
export function boundedDiagnosticMessage(value: unknown, fallback = "operation_failed"): string {
  let message = value instanceof Error ? value.message : value == null ? "" : String(value);
  message = message.replaceAll("\0", "\uFFFD").trim();
  if (!message && value instanceof Error) message = value.name;

  const normalizedFallback = fallback.replaceAll("\0", "\uFFFD").trim() || "operation_failed";
  return Array.from(message || normalizedFallback)
    .slice(0, PERSISTED_DIAGNOSTIC_MAX_CHARACTERS)
    .join("");
}

/** Normalize an optional diagnostic while preserving an absent value. */
export function optionalBoundedDiagnosticMessage(value: unknown): string | null {
  return value == null ? null : boundedDiagnosticMessage(value);
}

export function diagnosticErrorCode(value: unknown): string | null {
  try {
    if (!isBursarError(value)) return null;
    return isBursarErrorCode(value.code) ? value.code : null;
  } catch {
    return null;
  }
}

/** Return a fixed error type without consulting mutable names or constructors. */
export function diagnosticErrorType(value: unknown): string {
  try {
    if (isBursarError(value)) return "BursarError";
    if (value instanceof AggregateError) return "AggregateError";
    if (value instanceof TypeError) return "TypeError";
    if (value instanceof RangeError) return "RangeError";
    if (value instanceof ReferenceError) return "ReferenceError";
    if (value instanceof SyntaxError) return "SyntaxError";
    if (value instanceof URIError) return "URIError";
    if (value instanceof EvalError) return "EvalError";
    if (value instanceof Error) return "Error";
  } catch {
    return "UnknownError";
  }
  return "UnknownError";
}

/**
 * Return a persistence-safe diagnostic that never includes an exception
 * message, payload fragment, URL, identifier, or arbitrary metadata.
 */
export function persistedDiagnosticSummary(value: unknown, fallback = "operation_failed"): string {
  const operation = boundedDiagnosticMessage(fallback, "operation_failed")
    .replaceAll(/[^A-Za-z0-9_.-]/gu, "_")
    .slice(0, DIAGNOSTIC_CODE_MAX_CHARACTERS);
  return `${operation || "operation_failed"}:${diagnosticErrorCode(value) ?? diagnosticErrorType(value)}`;
}
import { isBursarError, isBursarErrorCode } from "../errors.js";
