import {
  type BursarError,
  isBursarError,
  StoreError,
  StoreTimeoutError,
  StoreUnavailableError,
} from "../errors.js";

export type PostgresOperationPhase =
  | "connect"
  | "begin"
  | "configure"
  | "query"
  | "commit"
  | "rollback"
  | "close"
  | "pool";

export interface PostgresErrorContext {
  operation?: string;
  phase?: PostgresOperationPhase;
  indeterminate?: boolean;
  rollbackFailed?: boolean;
}

const RETRYABLE_SQLSTATES = new Set([
  "40001", // serialization_failure
  "40P01", // deadlock_detected
  "55P03", // lock_not_available
  "57P01", // admin_shutdown
  "57P02", // crash_shutdown
  "57P03", // cannot_connect_now
  "53300", // too_many_connections
]);

const RETRYABLE_NETWORK_CODES = new Set([
  "EAI_AGAIN",
  "ECONNREFUSED",
  "ECONNRESET",
  "EHOSTDOWN",
  "EHOSTUNREACH",
  "ENETDOWN",
  "ENETRESET",
  "ENETUNREACH",
  "ENOTFOUND",
  "EPIPE",
  "ESOCKETTIMEDOUT",
  "ETIMEDOUT",
]);

function errorProperty(error: unknown, property: string): unknown {
  if (typeof error !== "object" || error === null) return undefined;
  try {
    return (error as Record<string, unknown>)[property];
  } catch {
    return undefined;
  }
}

function errorCode(error: unknown): string | undefined {
  const code = errorProperty(error, "code");
  return typeof code === "string" && code.length > 0 ? code.toUpperCase() : undefined;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  try {
    return String(error);
  } catch {
    return "Unknown PostgreSQL failure";
  }
}

function isSqlState(code: string | undefined): code is string {
  return code != null && !RETRYABLE_NETWORK_CODES.has(code) && /^[0-9A-Z]{5}$/.test(code);
}

function isTimeout(error: unknown, code: string | undefined): boolean {
  if (code === "57014" || code === "ETIMEDOUT" || code === "ESOCKETTIMEDOUT") return true;
  const message = errorMessage(error);
  return (
    message === "Query read timeout" ||
    message === "timeout expired" ||
    message === "timeout exceeded when trying to connect" ||
    message === "Connection terminated due to connection timeout" ||
    /\b(?:query|connection|statement) timed? ?out\b/i.test(message)
  );
}

function isUnavailable(error: unknown, code: string | undefined): boolean {
  if (code?.startsWith("08")) return true;
  if (code && (RETRYABLE_SQLSTATES.has(code) || RETRYABLE_NETWORK_CODES.has(code))) return true;
  return /^Connection terminated (?:unexpectedly|due to connection timeout)/i.test(
    errorMessage(error),
  );
}

function hasIndeterminateOutcome(error: unknown, code: string | undefined): boolean {
  if (code && RETRYABLE_NETWORK_CODES.has(code)) return true;
  if (code?.startsWith("08")) return true;
  if (code === undefined && errorMessage(error) === "Query read timeout") return true;
  return /^Connection terminated (?:unexpectedly|due to connection timeout)/i.test(
    errorMessage(error),
  );
}

/** Convert pg/network failures into the SDK's stable, cause-preserving taxonomy. */
export function normalizePostgresError(
  error: unknown,
  context: PostgresErrorContext = {},
): BursarError {
  if (isBursarError(error)) return error;

  const code = errorCode(error);
  const operation = context.operation ?? "operation";
  const details = {
    datastore: "postgresql",
    operation,
    ...(context.phase ? { phase: context.phase } : {}),
    ...(isSqlState(code) ? { sqlState: code } : {}),
    ...(code && !isSqlState(code) ? { networkCode: code } : {}),
    ...(context.rollbackFailed ? { rollbackFailed: true } : {}),
  };
  const options = {
    cause: error,
    details,
    indeterminate: (context.indeterminate ?? false) && hasIndeterminateOutcome(error, code),
  };

  if (isTimeout(error, code)) {
    return new StoreTimeoutError(`PostgreSQL ${operation} timed out`, options);
  }
  if (isUnavailable(error, code)) {
    return new StoreUnavailableError(`PostgreSQL ${operation} is temporarily unavailable`, options);
  }
  return new StoreError(`PostgreSQL ${operation} failed`, options);
}
