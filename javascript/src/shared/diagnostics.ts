import { isBursarError, isBursarErrorCode } from "../errors.js";

/** Maximum diagnostic length accepted by the SQL layer. */
export const PERSISTED_DIAGNOSTIC_MAX_CHARACTERS = 8_192;

const DIAGNOSTIC_CODE_MAX_CHARACTERS = 128;

/** Return trimmed, non-empty text accepted by diagnostic SQL constraints. */
export function boundedDiagnosticMessage(cause: unknown, fallback = "operation_failed"): string {
  let message = "";
  if (cause instanceof Error) message = cause.message;
  else if (cause !== null && cause !== undefined) {
    try {
      message = String(cause);
    } catch {
      message = "";
    }
  }
  message = message.replaceAll("\0", "\uFFFD").trim();
  if (!message && cause instanceof Error) message = cause.name;

  const normalizedFallback = fallback.replaceAll("\0", "\uFFFD").trim() || "operation_failed";
  return Array.from(message || normalizedFallback)
    .slice(0, PERSISTED_DIAGNOSTIC_MAX_CHARACTERS)
    .join("");
}

/** Normalize an optional diagnostic while preserving an absent value. */
export function optionalBoundedDiagnosticMessage(cause: unknown): string | null {
  return cause == null ? null : boundedDiagnosticMessage(cause);
}

export function diagnosticErrorCode(cause: unknown): string | null {
  try {
    if (!isBursarError(cause)) return null;
    return isBursarErrorCode(cause.code) ? cause.code : null;
  } catch {
    return null;
  }
}

/** Return a fixed error type without consulting mutable names or constructors. */
export function diagnosticErrorType(cause: unknown): string {
  try {
    if (isBursarError(cause)) return "BursarError";
    if (cause instanceof AggregateError) return "AggregateError";
    if (cause instanceof TypeError) return "TypeError";
    if (cause instanceof RangeError) return "RangeError";
    if (cause instanceof ReferenceError) return "ReferenceError";
    if (cause instanceof SyntaxError) return "SyntaxError";
    if (cause instanceof URIError) return "URIError";
    if (cause instanceof EvalError) return "EvalError";
    if (cause instanceof Error) return "Error";
  } catch {
    return "UnknownError";
  }
  return "UnknownError";
}

/**
 * Return a persistence-safe diagnostic that never includes an exception
 * message, payload fragment, URL, identifier, or arbitrary metadata.
 */
export function persistedDiagnosticSummary(cause: unknown, fallback = "operation_failed"): string {
  const operation = boundedDiagnosticMessage(fallback, "operation_failed")
    .replaceAll(/[^A-Za-z0-9_.-]/gu, "_")
    .slice(0, DIAGNOSTIC_CODE_MAX_CHARACTERS);
  return `${operation || "operation_failed"}:${diagnosticErrorCode(cause) ?? diagnosticErrorType(cause)}`;
}
