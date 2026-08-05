/** Maximum diagnostic length accepted by the SQL layer. */
export const PERSISTED_DIAGNOSTIC_MAX_CHARACTERS = 8_192;

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
