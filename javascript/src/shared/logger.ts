import type { StructuredValue } from "./json.js";

export type LogContextValue = StructuredValue | Date | Error;

export interface LogContext {
  [key: string]: LogContextValue;
}

export interface Logger {
  debug?: (message: string, context?: LogContext) => void;
  info?: (message: string, context?: LogContext) => void;
  warn?: (message: string, context?: LogContext) => void;
  error?: (message: string, context?: LogContext) => void;
}

export type NormalizedLogger = Required<Logger>;

export const noopLogger: NormalizedLogger = {
  debug: () => {},
  info: () => {},
  warn: () => {},
  error: () => {},
};

export function normalizeLogger(logger?: Logger | null): NormalizedLogger {
  return {
    debug: logger?.debug ?? noopLogger.debug,
    info: logger?.info ?? noopLogger.info,
    warn: logger?.warn ?? noopLogger.warn,
    error: logger?.error ?? noopLogger.error,
  };
}
