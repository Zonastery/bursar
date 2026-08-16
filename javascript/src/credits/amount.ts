import { Decimal } from "decimal.js";
import { z } from "zod";

import type { ExactAmount, MetricsOrAmount } from "./service-types.js";

/** Parse an exact public credit amount without accepting IEEE-754 numbers. */
export function toDecimal(value: ExactAmount): Decimal {
  let amount: Decimal;
  if (value instanceof Decimal) {
    amount = value;
  } else if (z.string().min(1).safeParse(value).success) {
    try {
      amount = new Decimal(z.string().parse(value));
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
  return value instanceof Decimal || z.string().safeParse(value).success;
}

/** Enforce the exact-money contract for untyped JavaScript callers. */
export function rejectNativeCreditAmount(value: MetricsOrAmount): void {
  if (z.number().safeParse(value).success) {
    throw new TypeError("amount must be a Decimal or decimal string");
  }
}
