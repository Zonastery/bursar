import {
  asArray,
  asDecimal,
  asInteger,
  asObject,
  asString,
  asStringArray,
  parseAvailability,
  parseExpiry,
  semanticError,
  validateIdentifiers,
  validateRegions,
} from "./parse-utils.js";
import type { JsonValue } from "../shared/json.js";
import type { CreditPolicy, CreditsConfig, GrantProgram } from "./types.js";
import { z } from "zod";

const grantTriggerSchema = z.enum([
  "account_created",
  "referral_completed",
  "promo_code_redeemed",
  "manual",
]);
const recipientSchema = z.enum(["subject", "referrer"]);
const idempotencyScopeSchema = z.enum(["subject", "event"]);

export function parseCredits(value: JsonValue): CreditsConfig {
  const raw = asObject(value);
  const bucketsRaw = asObject(raw.buckets ?? {});
  const policiesRaw = asObject(raw.policies ?? {});
  const programsRaw = asObject(raw.grant_programs ?? {});
  const displayRaw = raw.display == null ? null : asObject(raw.display);
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
    const awards = asArray(program.awards, `credits.grant_programs.${key}.awards`).map(
      (awardInput) => {
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
        const parsedAward: GrantProgram["awards"][number] = {
          recipient: recipientSchema.parse(award.recipient ?? "subject"),
          amount: asDecimal(award.amount),
          bucket,
        };
        if (parsedExpiry) parsedAward.expiry = parsedExpiry;
        return parsedAward;
      },
    );
    const eligibilityRaw = asObject(program.eligibility ?? {});
    const eligibilityRegions =
      eligibilityRaw.regions == null
        ? []
        : asStringArray(
            eligibilityRaw.regions,
            `credits.grant_programs.${key}.eligibility.regions`,
          );
    validateRegions(eligibilityRegions, `credits.grant_programs.${key}.eligibility.regions`);
    if (
      program.trigger !== "referral_completed" &&
      awards.some((award) => award.recipient === "referrer")
    ) {
      semanticError(`grant program '${key}' referrer awards require referral_completed`);
    }
    const parsedProgram: GrantProgram = {
      trigger: grantTriggerSchema.parse(program.trigger),
      awards,
      eligibility: {
        plans:
          eligibilityRaw.plans == null
            ? []
            : asStringArray(
                eligibilityRaw.plans,
                `credits.grant_programs.${key}.eligibility.plans`,
              ),
        regions: eligibilityRegions,
      },
      maxAwardsPerSubject: asInteger(program.max_awards_per_subject ?? 1),
      idempotencyScope: idempotencyScopeSchema.parse(program.idempotency_scope ?? "subject"),
    };
    if (program.availability != null) {
      parsedProgram.availability = parseAvailability(program.availability);
    }
    grantPrograms[key] = parsedProgram;
  }

  const result: CreditsConfig = {
    buckets,
    policies,
    grantPrograms,
  };
  if (defaultBucket != null) result.defaultBucket = defaultBucket;
  if (displayRaw) {
    result.display = {
      currency: asString(displayRaw.currency).toUpperCase(),
      unitsPerMajor: asDecimal(displayRaw.units_per_major),
    };
  }
  return result;
}
