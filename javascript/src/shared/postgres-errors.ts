import { z } from "zod";

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

function errorCode(cause: unknown): string | undefined {
  try {
    const parsed = z.object({ code: z.string().optional() }).safeParse(cause);
    const code = parsed.success ? parsed.data.code : undefined;
    return code && code.length > 0 ? code.toUpperCase() : undefined;
  } catch {
    return undefined;
  }
}

function errorMessage(cause: unknown): string {
  if (cause instanceof Error) return cause.message;
  try {
    return String(cause);
  } catch {
    return "Unknown PostgreSQL failure";
  }
}

function isSqlState(code: string | undefined): code is string {
  return code != null && !RETRYABLE_NETWORK_CODES.has(code) && /^[0-9A-Z]{5}$/.test(code);
}

function isTimeout(cause: unknown, code: string | undefined): boolean {
  if (code === "57014" || code === "ETIMEDOUT" || code === "ESOCKETTIMEDOUT") return true;
  const message = errorMessage(cause);
  return (
    message === "Query read timeout" ||
    message === "timeout expired" ||
    message === "timeout exceeded when trying to connect" ||
    message === "Connection terminated due to connection timeout" ||
    /\b(?:query|connection|statement) timed? ?out\b/i.test(message)
  );
}

function isUnavailable(cause: unknown, code: string | undefined): boolean {
  if (code?.startsWith("08")) return true;
  if (code && (RETRYABLE_SQLSTATES.has(code) || RETRYABLE_NETWORK_CODES.has(code))) return true;
  return /^Connection terminated (?:unexpectedly|due to connection timeout)/i.test(
    errorMessage(cause),
  );
}

function hasIndeterminateOutcome(cause: unknown, code: string | undefined): boolean {
  if (code && RETRYABLE_NETWORK_CODES.has(code)) return true;
  if (code?.startsWith("08")) return true;
  if (code === undefined && errorMessage(cause) === "Query read timeout") return true;
  return /^Connection terminated (?:unexpectedly|due to connection timeout)/i.test(
    errorMessage(cause),
  );
}

/** Convert pg/network failures into the SDK's stable, cause-preserving taxonomy. */
export function normalizePostgresError(
  cause: unknown,
  context: PostgresErrorContext = {},
): BursarError {
  if (isBursarError(cause)) return cause;

  const code = errorCode(cause);
  const operation = context.operation ?? "operation";
  const details = {
    datastore: "postgresql",
    operation,
  };
  if (context.phase) Object.assign(details, { phase: context.phase });
  if (isSqlState(code)) Object.assign(details, { sqlState: code });
  if (code && !isSqlState(code)) Object.assign(details, { networkCode: code });
  if (context.rollbackFailed) Object.assign(details, { rollbackFailed: true });
  const options = {
    cause,
    details,
    indeterminate: (context.indeterminate ?? false) && hasIndeterminateOutcome(cause, code),
  };

  if (isTimeout(cause, code)) {
    return new StoreTimeoutError(`PostgreSQL ${operation} timed out`, options);
  }
  if (isUnavailable(cause, code)) {
    return new StoreUnavailableError(`PostgreSQL ${operation} is temporarily unavailable`, options);
  }
  return new StoreError(`PostgreSQL ${operation} failed`, options);
}
