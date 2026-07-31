import { asInteger, asObject, semanticError, validateIdentifiers } from "./parse-utils.js";
import type { AdmissionPolicy, EntitlementsConfig, FeatureDefinition } from "./types.js";

function parseFeatureDefinition(value: unknown, path: string): FeatureDefinition {
  const feature = asObject(value);
  if (feature.type === "enum") {
    const values = feature.values as string[];
    if (new Set(values).size !== values.length) {
      semanticError(`${path}.values must be unique`);
    }
    if (!values.includes(String(feature.default))) {
      semanticError(`${path}.default must be one of values`);
    }
  }
  if (feature.type === "integer") {
    const defaultValue = asInteger(feature.default);
    const minimum = feature.minimum == null ? undefined : asInteger(feature.minimum);
    const maximum = feature.maximum == null ? undefined : asInteger(feature.maximum);
    if (minimum != null && maximum != null && minimum > maximum) {
      semanticError(`${path}.minimum cannot exceed maximum`);
    }
    if (minimum != null && defaultValue < minimum) {
      semanticError(`${path}.default is below minimum`);
    }
    if (maximum != null && defaultValue > maximum) {
      semanticError(`${path}.default exceeds maximum`);
    }
  }
  if (feature.type === "string" && feature.pattern != null) {
    try {
      new RegExp(String(feature.pattern));
    } catch {
      semanticError(`${path}.pattern must be a valid regular expression`);
    }
  }
  return { ...feature, type: feature.type } as FeatureDefinition;
}

export function parseEntitlements(value: unknown): EntitlementsConfig {
  const raw = asObject(value ?? {});
  const featuresRaw = asObject(raw.features ?? {});
  validateIdentifiers(featuresRaw, "entitlements.features");
  return {
    features: Object.fromEntries(
      Object.entries(featuresRaw).map(([key, input]) => [
        key,
        parseFeatureDefinition(input, `entitlements.features.${key}`),
      ]),
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
