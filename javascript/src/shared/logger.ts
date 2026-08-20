import type { StructuredValue } from "./json.js";

export type LogContextValue = StructuredValue | Date | Error;

export interface LogContext {
  [key: string]: LogContextValue;
}

export type LoggerMethod = (message: string, context?: LogContext) => void | PromiseLike<void>;

export interface Logger {
  debug?: LoggerMethod;
  info?: LoggerMethod;
  warn?: LoggerMethod;
  error?: LoggerMethod;
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
