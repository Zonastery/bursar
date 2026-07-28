import {
  asDecimal,
  asInteger,
  asObject,
  asString,
  parseAvailability,
  parseExpiry,
  semanticError,
  validateIdentifiers,
} from "./parse-utils.js";
import type { CreditPolicy, CreditsConfig, GrantProgram } from "./types.js";

export function parseCredits(value: unknown): CreditsConfig {
  const raw = asObject(value);
  const accountingRaw = asObject(raw.accounting);
  const bucketsRaw = asObject(raw.buckets ?? {});
  const policiesRaw = asObject(raw.policies ?? {});
  const programsRaw = asObject(raw.grant_programs ?? {});
  validateIdentifiers(bucketsRaw, "credits.buckets");
  validateIdentifiers(policiesRaw, "credits.policies");
  validateIdentifiers(programsRaw, "credits.grant_programs");

  const buckets = Object.fromEntries(
    Object.entries(bucketsRaw).map(([key, input]) => {
      const bucket = asObject(input);
      const parsedExpiry = parseExpiry(
        bucket.expiry ?? { type: "never" },
        `credits.buckets.${key}.expiry`,
      );
      if (parsedExpiry.type === "subscription_end") {
        semanticError(`credits.buckets.${key}.expiry cannot be subscription_end`);
      }
      return [key, { priority: asInteger(bucket.priority), expiry: parsedExpiry }];
    }),
  );
  const priorities = Object.values(buckets).map((bucket) => bucket.priority);
  if (new Set(priorities).size !== priorities.length) {
    semanticError("credits bucket priorities must be unique");
  }
  const defaultBucket = raw.default_bucket == null ? undefined : asString(raw.default_bucket);
  if (defaultBucket != null && !buckets[defaultBucket]) {
    semanticError("credits.default_bucket references an unknown bucket");
  }

  const policies: Record<string, CreditPolicy> = {};
  for (const [key, input] of Object.entries(policiesRaw)) {
    const policy = asObject(input);
    policies[key] =
      policy.type === "credit_line"
        ? { type: "credit_line", limit: asDecimal(policy.limit) }
        : { type: "prepaid" };
  }

  const grantPrograms: Record<string, GrantProgram> = {};
  for (const [key, input] of Object.entries(programsRaw)) {
    const program = asObject(input);
    const awards = (program.awards as unknown[]).map((awardInput) => {
      const award = asObject(awardInput);
      const bucket = asString(award.bucket);
      if (!buckets[bucket]) {
        semanticError(`grant program '${key}' references unknown bucket '${bucket}'`);
      }
      const parsedExpiry =
        award.expiry == null
          ? undefined
          : parseExpiry(award.expiry, `credits.grant_programs.${key}`);
      if (parsedExpiry?.type === "subscription_end") {
        semanticError(`grant program '${key}' cannot use subscription_end expiry`);
      }
      return {
        recipient: (award.recipient ?? "subject") as "subject" | "referrer",
        amount: asDecimal(award.amount),
        bucket,
        ...(parsedExpiry ? { expiry: parsedExpiry } : {}),
      };
    });
    const eligibilityRaw = asObject(program.eligibility ?? {});
    grantPrograms[key] = {
      trigger: program.trigger as GrantProgram["trigger"],
      awards,
      ...(program.availability == null
        ? {}
        : { availability: parseAvailability(program.availability) }),
      eligibility: {
        plans: (eligibilityRaw.plans ?? []) as string[],
        regions: (eligibilityRaw.regions ?? []) as string[],
      },
      maxAwardsPerSubject: asInteger(program.max_awards_per_subject ?? 1),
      idempotencyScope: (program.idempotency_scope ?? "subject") as "subject" | "event",
    };
  }

  return {
    accounting: {
      unit: accountingRaw.unit as "credit",
      scale: asInteger(accountingRaw.scale) as 6,
      rounding: accountingRaw.rounding as "half_up",
    },
    buckets,
    ...(defaultBucket == null ? {} : { defaultBucket }),
    policies,
    grantPrograms,
  };
}
