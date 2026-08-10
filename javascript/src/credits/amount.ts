import { Decimal } from "decimal.js";

import type { ExactAmount, MetricsOrAmount } from "./service-types.js";

/** Parse an exact public credit amount without accepting IEEE-754 numbers. */
export function toDecimal(value: ExactAmount): Decimal {
  let amount: Decimal;
  if (value instanceof Decimal) {
    amount = value;
  } else if (typeof value === "string" && value.trim().length > 0) {
    try {
      amount = new Decimal(value);
    } catch (cause) {
      throw new TypeError("amount must be a valid decimal string", { cause });
    }
  } else {
    throw new TypeError("amount must be a Decimal or decimal string");
  }
  if (!amount.isFinite()) throw new RangeError("amount must be finite");
  return amount;
}

export function isAmount(value: MetricsOrAmount): value is ExactAmount {
  return value instanceof Decimal || typeof value === "string";
}

/** Enforce the exact-money contract for untyped JavaScript callers. */
export function rejectNativeCreditAmount(value: unknown): void {
  if (typeof value === "number") {
    throw new TypeError("amount must be a Decimal or decimal string");
  }
}
