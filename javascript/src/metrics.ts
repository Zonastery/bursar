import type { Decimal } from "decimal.js";
import type { StructuredObject } from "./shared/json.js";

/** One provider-neutral billable operation. */
export interface UsageMetrics {
  operation: string;
  measures?: Record<string, number | string>;
  dimensions?: Record<string, string | number | boolean | Decimal.Value>;
  metadata?: StructuredObject;
}
