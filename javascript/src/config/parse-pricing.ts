import { validateExpression } from "../expr.js";
import { z } from "zod";
import {
  asArray,
  asBoolean,
  asDecimal,
  asObject,
  asString,
  identifier,
  semanticError,
  validateIdentifiers,
} from "./parse-utils.js";
import type { JsonValue } from "../shared/json.js";
import type {
  Charge,
  DimensionDefinition,
  DimensionMatcher,
  GraduatedTier,
  MatcherScalar,
  OperationDefinition,
  OperationPricing,
  PricingConfig,
  RateCard,
} from "./types.js";

const roundingSchema = z.enum(["ceil", "floor", "nearest"]);
const dimensionTypeSchema = z.enum(["string", "number", "boolean"]);

function parseMatcherScalar(
  value: JsonValue | undefined,
  definition: DimensionDefinition,
  path: string,
): MatcherScalar {
  if (definition.type === "boolean") {
    const parsed = z.boolean().safeParse(value);
    if (!parsed.success) semanticError(`${path} matcher values must be booleans`);
    return parsed.data;
  }
  if (definition.type === "number") {
    const parsed = z.union([z.number(), z.string()]).safeParse(value);
    if (!parsed.success) semanticError(`${path} matcher values must be decimal strings or numbers`);
    return asDecimal(parsed.data, path);
  }
  const parsed = z.string().safeParse(value);
  if (!parsed.success) semanticError(`${path} matcher values must be strings`);
  return parsed.data;
}

function parseMatcher(
  value: JsonValue,
  definition: DimensionDefinition,
  path: string,
): DimensionMatcher {
  const raw = asObject(value);
  switch (raw.op) {
    case "in":
    case "not_in":
      return {
        op: raw.op,
        values: asArray(raw.values, `${path}.values`).map((item) =>
          parseMatcherScalar(item, definition, path),
        ),
      };
    case "prefix":
      if (definition.type !== "string") {
        semanticError(`${path} prefix matcher requires a string dimension`);
      }
      return { op: "prefix", value: asString(raw.value) };
    case "range": {
      if (definition.type !== "number") {
        semanticError(`${path} range matcher requires a number dimension`);
      }
      const parsed: Extract<DimensionMatcher, { op: "range" }> = { op: "range" };
      if (raw.gt != null) parsed.gt = asDecimal(raw.gt);
      if (raw.gte != null) parsed.gte = asDecimal(raw.gte);
      if (raw.lt != null) parsed.lt = asDecimal(raw.lt);
      if (raw.lte != null) parsed.lte = asDecimal(raw.lte);
      if (parsed.gt == null && parsed.gte == null && parsed.lt == null && parsed.lte == null) {
        semanticError(`${path} range matcher requires at least one bound`);
      }
      if (parsed.gt != null && parsed.gte != null) {
        semanticError(`${path} range matcher cannot combine gt and gte`);
      }
      if (parsed.lt != null && parsed.lte != null) {
        semanticError(`${path} range matcher cannot combine lt and lte`);
      }
      const lower = parsed.gt ?? parsed.gte;
      const upper = parsed.lt ?? parsed.lte;
      if (lower != null && upper != null && lower.gte(upper)) {
        semanticError(`${path} range matcher lower bound must be less than upper bound`);
      }
      return parsed;
    }
    default:
      return { op: "eq", value: parseMatcherScalar(raw.value, definition, path) };
  }
}

function parseTiers(value: JsonValue | undefined): GraduatedTier[] {
  const tiers = asArray(value, "pricing.tiers").map((item) => {
    const tier = asObject(item);
    const parsedTier: GraduatedTier = { rate: asDecimal(tier.rate) };
    if (tier.up_to != null) parsedTier.upTo = asDecimal(tier.up_to);
    return parsedTier;
  });
  if (tiers.at(-1)?.upTo != null || tiers.slice(0, -1).some((tier) => tier.upTo == null)) {
    semanticError("graduated and volume tiers must end with exactly one open-ended tier");
  }
  const finite = tiers.flatMap((tier) => (tier.upTo == null ? [] : [tier.upTo]));
  if (
    finite.some((bound, index) => {
      const previous = finite[index - 1];
      return index > 0 && previous !== undefined && bound.lte(previous);
    })
  ) {
    semanticError("graduated and volume tier bounds must be strictly increasing");
  }
  return tiers;
}

function parseCharge(value: JsonValue): Charge {
  const raw = asObject(value);
  switch (raw.type) {
    case "flat":
      return { type: "flat", amount: asDecimal(raw.amount) };
    case "per_unit":
      return {
        type: "per_unit",
        measure: asString(raw.measure),
        rate: asDecimal(raw.rate),
        unitSize: asDecimal(raw.unit_size ?? 1),
      };
    case "package":
      return {
        type: "package",
        measure: asString(raw.measure),
        units: asDecimal(raw.units),
        amount: asDecimal(raw.amount),
        rounding: roundingSchema.parse(raw.rounding ?? "ceil"),
      };
    case "graduated":
    case "volume":
      return {
        type: raw.type,
        measure: asString(raw.measure),
        tiers: parseTiers(raw.tiers),
      };
    case "expression":
      return { type: "expression", formula: asString(raw.formula) };
    case "sum":
      return {
        type: "sum",
        components: asArray(raw.components, "pricing.sum.components").map(parseCharge),
      };
    default:
      return semanticError(`unsupported charge type '${asString(raw.type)}'`);
  }
}

function chargeMeasures(value: Charge): Set<string> {
  if ("measure" in value) return new Set([value.measure]);
  if (value.type === "sum") {
    return new Set(value.components.flatMap((component) => [...chargeMeasures(component)]));
  }
  return new Set();
}

function expressionCharges(value: Charge): Extract<Charge, { type: "expression" }>[] {
  if (value.type === "expression") return [value];
  if (value.type === "sum") return value.components.flatMap(expressionCharges);
  return [];
}

function validateCharge(
  parsed: Charge,
  definition: OperationDefinition,
  operationKey: string,
): void {
  const unknownMeasures = [...chargeMeasures(parsed)].filter((name) => !definition.measures[name]);
  if (unknownMeasures.length) {
    semanticError(
      `pricing for operation '${operationKey}' references undeclared measures ${unknownMeasures.join(", ")}`,
    );
  }
  for (const expression of expressionCharges(parsed)) {
    validateExpression(expression.formula, new Set(Object.keys(definition.measures)));
  }
}

function validateRateCardInheritance(rateCards: Record<string, RateCard>): void {
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (key: string): void => {
    if (visiting.has(key)) semanticError(`pricing rate-card inheritance cycle includes '${key}'`);
    if (visited.has(key)) return;
    const card = rateCards[key];
    if (!card) semanticError(`unknown rate card '${key}'`);
    visiting.add(key);
    if (card.extends) visit(card.extends);
    visiting.delete(key);
    visited.add(key);
  };
  Object.keys(rateCards).forEach(visit);
}

export function parsePricing(value: JsonValue): PricingConfig {
  const raw = asObject(value);
  const operationsRaw = asObject(raw.operations);
  const cardsRaw = asObject(raw.rate_cards);
  validateIdentifiers(operationsRaw, "pricing.operations");
  validateIdentifiers(cardsRaw, "pricing.rate_cards");
  if (Object.keys(operationsRaw).length === 0) {
    semanticError("pricing.operations must not be empty");
  }
  if (Object.keys(cardsRaw).length === 0) {
    semanticError("pricing.rate_cards must not be empty");
  }

  const operations: Record<string, OperationDefinition> = {};
  for (const [operationKey, input] of Object.entries(operationsRaw)) {
    const operation = asObject(input);
    const measuresRaw = asObject(operation.measures);
    const dimensionsRaw = asObject(operation.dimensions ?? {});
    validateIdentifiers(measuresRaw, `pricing.operations.${operationKey}.measures`);
    validateIdentifiers(dimensionsRaw, `pricing.operations.${operationKey}.dimensions`);
    if (Object.keys(measuresRaw).length === 0) {
      semanticError(`pricing.operations.${operationKey}.measures must not be empty`);
    }
    const overlap = Object.keys(measuresRaw).filter((key) =>
      Object.prototype.hasOwnProperty.call(dimensionsRaw, key),
    );
    if (overlap.length) {
      semanticError(`operation '${operationKey}' reuses names as measures and dimensions`);
    }
    operations[operationKey] = {
      measures: Object.fromEntries(
        Object.entries(measuresRaw).map(([key, definition]) => [
          key,
          { unit: identifier(asString(asObject(definition).unit), `${operationKey}.${key}.unit`) },
        ]),
      ),
      dimensions: Object.fromEntries(
        Object.entries(dimensionsRaw).map(([key, definition]) => {
          const item = asObject(definition);
          return [
            key,
            {
              type: dimensionTypeSchema.parse(item.type),
              required: asBoolean(item.required ?? true),
            },
          ];
        }),
      ),
    };
  }

  const rateCards: Record<string, RateCard> = {};
  for (const [cardKey, input] of Object.entries(cardsRaw)) {
    const rawCard = asObject(input);
    const operationPricesRaw = asObject(rawCard.operations ?? {});
    const operationPrices: Record<string, OperationPricing> = {};
    for (const [operationKey, operationInput] of Object.entries(operationPricesRaw)) {
      const definition = operations[operationKey];
      if (!definition) {
        semanticError(`rate card '${cardKey}' references unknown operation '${operationKey}'`);
      }
      const operationPriceRaw = asObject(operationInput);
      const rules = asArray(operationPriceRaw.rules ?? [], "pricing.rules").map(
        (ruleInput, index) => {
          const ruleRaw = asObject(ruleInput);
          const whenRaw = asObject(ruleRaw.when);
          const unknownDimensions = Object.keys(whenRaw).filter(
            (name) => !definition.dimensions[name],
          );
          if (unknownDimensions.length) {
            semanticError(
              `pricing.rate_cards.${cardKey}.operations.${operationKey}.rules[${index}] matches undeclared dimensions ${unknownDimensions.join(", ")}`,
            );
          }
          const parsedCharge = parseCharge(
            asObject(
              ruleRaw.charge,
              `pricing.rate_cards.${cardKey}.operations.${operationKey}.rules[${index}].charge`,
            ),
          );
          validateCharge(parsedCharge, definition, operationKey);
          return {
            when: Object.fromEntries(
              Object.entries(whenRaw).map(([key, item]) => {
                const dimension = definition.dimensions[key];
                if (!dimension) {
                  semanticError(
                    `pricing.rate_cards.${cardKey}.operations.${operationKey}.rules[${index}] matches undeclared dimension '${key}'`,
                  );
                }
                return [
                  key,
                  parseMatcher(
                    item,
                    dimension,
                    `pricing.rate_cards.${cardKey}.operations.${operationKey}.rules[${index}].when.${key}`,
                  ),
                ];
              }),
            ),
            charge: parsedCharge,
          };
        },
      );
      const unmatchedRaw = asObject(operationPriceRaw.unmatched);
      const unmatched =
        unmatchedRaw.action === "charge"
          ? ({ action: "charge", charge: parseCharge(asObject(unmatchedRaw.charge)) } as const)
          : ({ action: "reject" } as const);
      if (unmatched.action === "charge") {
        validateCharge(unmatched.charge, definition, operationKey);
      }
      operationPrices[operationKey] = { rules, unmatched };
    }
    const rateCard: RateCard = { operations: operationPrices };
    if (rawCard.extends != null) rateCard.extends = asString(rawCard.extends);
    rateCards[cardKey] = rateCard;
  }

  validateRateCardInheritance(rateCards);
  return { operations, rateCards };
}

export function resolvesOperation(
  pricing: PricingConfig,
  cardKey: string,
  operation: string,
): boolean {
  const card = pricing.rateCards[cardKey];
  if (!card) return false;
  return (
    Object.prototype.hasOwnProperty.call(card.operations, operation) ||
    (card.extends != null && resolvesOperation(pricing, card.extends, operation))
  );
}
