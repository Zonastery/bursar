import { asInteger, asObject, validateIdentifiers } from "./parse-utils.js";
import type { AdmissionPolicy, EntitlementsConfig, FeatureDefinition } from "./types.js";

export function parseEntitlements(value: unknown): EntitlementsConfig {
  const raw = asObject(value ?? {});
  const featuresRaw = asObject(raw.features ?? {});
  validateIdentifiers(featuresRaw, "entitlements.features");
  return {
    features: Object.fromEntries(
      Object.entries(featuresRaw).map(([key, input]) => {
        const feature = asObject(input);
        return [key, { ...feature, type: feature.type } as FeatureDefinition];
      }),
    ),
  };
}

export function parseAdmission(value: unknown): {
  policies: Record<string, AdmissionPolicy>;
} {
  const raw = asObject(value ?? {});
  const policiesRaw = asObject(raw.policies ?? {});
  validateIdentifiers(policiesRaw, "admission.policies");
  return {
    policies: Object.fromEntries(
      Object.entries(policiesRaw).map(([key, input]) => {
        const policy = asObject(input);
        const operationsRaw = asObject(policy.operations ?? {});
        validateIdentifiers(operationsRaw, `admission.policies.${key}.operations`);
        return [
          key,
          {
            ...(policy.max_in_flight == null
              ? {}
              : { maxInFlight: asInteger(policy.max_in_flight) }),
            operations: Object.fromEntries(
              Object.entries(operationsRaw).map(([operation, definition]) => [
                operation,
                { maxInFlight: asInteger(asObject(definition).max_in_flight) },
              ]),
            ),
          },
        ];
      }),
    ),
  };
}
