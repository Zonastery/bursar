import {
  asBoolean,
  asInteger,
  asObject,
  asString,
  asStringArray,
  semanticError,
  validateIdentifiers,
} from "./parse-utils.js";
import type { JsonValue } from "../shared/json.js";
import type { AdmissionPolicy, EntitlementsConfig, FeatureDefinition } from "./types.js";

type ParsedAdmission = { policies: Record<string, AdmissionPolicy> };

function parseFeatureDefinition(value: JsonValue, path: string): FeatureDefinition {
  const feature = asObject(value);
  const type = asString(feature.type, `${path}.type`);
  switch (type) {
    case "boolean":
      return { type, default: asBoolean(feature.default, `${path}.default`) };
    case "enum": {
      const values = asStringArray(feature.values, `${path}.values`);
      if (new Set(values).size !== values.length) semanticError(`${path}.values must be unique`);
      const defaultValue = asString(feature.default, `${path}.default`);
      if (!values.includes(defaultValue)) semanticError(`${path}.default must be one of values`);
      return { type, values, default: defaultValue };
    }
    case "integer": {
      const defaultValue = asInteger(feature.default, `${path}.default`);
      const minimum = feature.minimum == null ? undefined : asInteger(feature.minimum);
      const maximum = feature.maximum == null ? undefined : asInteger(feature.maximum);
      if (minimum != null && maximum != null && minimum > maximum) {
        semanticError(`${path}.minimum cannot exceed maximum`);
      }
      if (minimum != null && defaultValue < minimum)
        semanticError(`${path}.default is below minimum`);
      if (maximum != null && defaultValue > maximum) {
        semanticError(`${path}.default exceeds maximum`);
      }
      const result: Extract<FeatureDefinition, { type: "integer" }> = {
        type,
        default: defaultValue,
      };
      if (minimum != null) result.minimum = minimum;
      if (maximum != null) result.maximum = maximum;
      return result;
    }
    case "string": {
      const defaultValue = asString(feature.default, `${path}.default`);
      const pattern =
        feature.pattern == null ? undefined : asString(feature.pattern, `${path}.pattern`);
      if (pattern != null) {
        try {
          new RegExp(pattern);
        } catch {
          semanticError(`${path}.pattern must be a valid regular expression`);
        }
      }
      return pattern == null
        ? { type, default: defaultValue }
        : { type, default: defaultValue, pattern };
    }
    default:
      semanticError(`${path}.type must be a supported feature type`);
  }
}

export function parseEntitlements(value: JsonValue | undefined): EntitlementsConfig {
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

export function parseAdmission(value: JsonValue | undefined): ParsedAdmission {
  const raw = asObject(value ?? {});
  const policiesRaw = asObject(raw.policies ?? {});
  validateIdentifiers(policiesRaw, "admission.policies");
  return {
    policies: Object.fromEntries(
      Object.entries(policiesRaw).map(([key, input]) => {
        const policy = asObject(input);
        const operationsRaw = asObject(policy.operations ?? {});
        validateIdentifiers(operationsRaw, `admission.policies.${key}.operations`);
        const parsedPolicy: AdmissionPolicy = {
          operations: Object.fromEntries(
            Object.entries(operationsRaw).map(([operation, definition]) => [
              operation,
              { maxInFlight: asInteger(asObject(definition).max_in_flight) },
            ]),
          ),
        };
        if (policy.max_in_flight != null) {
          parsedPolicy.maxInFlight = asInteger(policy.max_in_flight);
        }
        return [key, parsedPolicy];
      }),
    ),
  };
}
