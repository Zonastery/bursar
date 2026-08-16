import { createHash } from "node:crypto";
import { z } from "zod";

const MAX_IDEMPOTENCY_KEY_LENGTH = 255;
const INTERNAL_SCOPE_RE = /^[a-z][a-z0-9-]{0,47}$/u;
type StableKeyInput = string | null | undefined;

/** Validate a caller-owned replay key without silently rewriting it. */
const stableKeySchema = z
  .string()
  .min(1)
  .refine((value) => value === value.trim())
  .refine((value) => Array.from(value).length <= MAX_IDEMPOTENCY_KEY_LENGTH);

export function requireStableKey(value: StableKeyInput, field = "idempotencyKey"): string {
  const parsed = stableKeySchema.safeParse(value);
  if (!parsed.success) {
    throw new TypeError(`${field} must be a trimmed non-empty string of at most 255 characters`);
  }
  return parsed.data;
}

/**
 * Namespace a stable key without truncating it or creating prefix collisions.
 * Dynamic identifiers participate in the candidate hash, while the bounded
 * internal scope keeps the fallback readable and within provider limits.
 */
export function scopedStableKey(
  value: StableKeyInput,
  scope: string,
  components: readonly string[] = [],
): string {
  const key = requireStableKey(value);
  const parsedScope = z.string().regex(INTERNAL_SCOPE_RE).safeParse(scope);
  if (!parsedScope.success) {
    throw new TypeError("scope must be a short lowercase internal label");
  }
  const parsedComponents = z
    .array(
      z
        .string()
        .min(1)
        .refine((component) => component === component.trim()),
    )
    .safeParse(components);
  if (!parsedComponents.success) {
    throw new TypeError("scope components must be non-empty trimmed strings");
  }
  const encodedComponents = parsedComponents.data.map(
    (component) => `${Array.from(component).length}#${component}`,
  );
  const candidate = [key, parsedScope.data, ...encodedComponents].join(":");
  if (Array.from(candidate).length <= MAX_IDEMPOTENCY_KEY_LENGTH) return candidate;
  const digest = createHash("sha256").update(candidate, "utf8").digest("hex");
  return `bursar:${parsedScope.data}:${digest}`;
}
