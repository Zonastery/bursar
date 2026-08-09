import { createHash } from "node:crypto";

const MAX_IDEMPOTENCY_KEY_LENGTH = 255;
const INTERNAL_SCOPE_RE = /^[a-z][a-z0-9-]{0,47}$/u;

/** Validate a caller-owned replay key without silently rewriting it. */
export function requireStableKey(value: unknown, field = "idempotencyKey"): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value !== value.trim() ||
    Array.from(value).length > MAX_IDEMPOTENCY_KEY_LENGTH
  ) {
    throw new TypeError(`${field} must be a trimmed non-empty string of at most 255 characters`);
  }
  return value;
}

/**
 * Namespace a stable key without truncating it or creating prefix collisions.
 * Dynamic identifiers participate in the candidate hash, while the bounded
 * internal scope keeps the fallback readable and within provider limits.
 */
export function scopedStableKey(
  value: unknown,
  scope: string,
  components: readonly string[] = [],
): string {
  const key = requireStableKey(value);
  if (!INTERNAL_SCOPE_RE.test(scope)) {
    throw new TypeError("scope must be a short lowercase internal label");
  }
  if (
    components.some(
      (component) => typeof component !== "string" || !component || component !== component.trim(),
    )
  ) {
    throw new TypeError("scope components must be non-empty trimmed strings");
  }
  const encodedComponents = components.map(
    (component) => `${Array.from(component).length}#${component}`,
  );
  const candidate = [key, scope, ...encodedComponents].join(":");
  if (Array.from(candidate).length <= MAX_IDEMPOTENCY_KEY_LENGTH) return candidate;
  const digest = createHash("sha256").update(candidate, "utf8").digest("hex");
  return `bursar:${scope}:${digest}`;
}
