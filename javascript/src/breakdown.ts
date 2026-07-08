import Decimal from "decimal.js";
import { quantizeMoney } from "./expr.js";

/**
 * Granular credit cost report for a usage event.
 *
 * All money fields are `Decimal` (never binary `number`) and are quantized to
 * 4dp ROUND_HALF_UP. `total` is the single source of truth: it is recomputed
 * from the components (clamped at 0) and quantized — there is NO implicit
 * integer truncation of the total (that was the revenue-leak bug).
 */
export interface CostBreakdown {
  modelCredits: Decimal;
  toolCredits: Decimal;
  searchCredits: Decimal;
  cacheSavings: Decimal;
  fixedCredits: Decimal;
  total: Decimal;
  breakdown: Record<string, unknown>;
}

export function makeCostBreakdown(partial?: {
  modelCredits?: Decimal;
  toolCredits?: Decimal;
  searchCredits?: Decimal;
  cacheSavings?: Decimal;
  fixedCredits?: Decimal;
  breakdown?: Record<string, unknown>;
}): CostBreakdown {
  const modelCredits = quantizeMoney(partial?.modelCredits ?? new Decimal(0));
  const toolCredits = quantizeMoney(partial?.toolCredits ?? new Decimal(0));
  const searchCredits = quantizeMoney(partial?.searchCredits ?? new Decimal(0));
  const cacheSavings = quantizeMoney(partial?.cacheSavings ?? new Decimal(0));
  const fixedCredits = quantizeMoney(partial?.fixedCredits ?? new Decimal(0));

  // Single source of truth for the total: sum of components, clamped at 0,
  // quantized to 4dp HALF_UP.
  const rawTotal = modelCredits
    .plus(toolCredits)
    .plus(searchCredits)
    .plus(fixedCredits)
    .plus(cacheSavings);
  const total = quantizeMoney(Decimal.max(new Decimal(0), rawTotal));

  return {
    modelCredits,
    toolCredits,
    searchCredits,
    cacheSavings,
    fixedCredits,
    total,
    breakdown: partial?.breakdown ?? {},
  };
}
