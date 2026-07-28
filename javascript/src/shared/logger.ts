export interface Logger {
  debug?: (message: string, context?: Record<string, unknown>) => void;
  info?: (message: string, context?: Record<string, unknown>) => void;
  warn?: (message: string, context?: Record<string, unknown>) => void;
  error?: (message: string, context?: Record<string, unknown>) => void;
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
