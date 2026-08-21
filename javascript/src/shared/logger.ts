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

function isolateLoggerMethod(method: LoggerMethod, receiver: Logger): LoggerMethod {
  return (message, context) => {
    let result: void | PromiseLike<void>;
    try {
      result = method.call(receiver, message, context);
    } catch {
      return;
    }
    // Logging is diagnostic side effect only. A rejected injected logger must
    // never become an unhandled rejection or change billing behavior.
    if (result != null) void Promise.resolve(result).catch(() => {});
  };
}

export function normalizeLogger(logger?: Logger | null): NormalizedLogger {
  const source: Logger = logger ?? {};
  return {
    debug:
      typeof source.debug === "function"
        ? isolateLoggerMethod(source.debug, source)
        : noopLogger.debug,
    info:
      typeof source.info === "function"
        ? isolateLoggerMethod(source.info, source)
        : noopLogger.info,
    warn:
      typeof source.warn === "function"
        ? isolateLoggerMethod(source.warn, source)
        : noopLogger.warn,
    error:
      typeof source.error === "function"
        ? isolateLoggerMethod(source.error, source)
        : noopLogger.error,
  };
}
