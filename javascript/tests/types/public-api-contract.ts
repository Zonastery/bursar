import type DodoPayments from "dodopayments";

import type {
  BillingEvent,
  Bursar,
  BursarConfigData,
  canonicalBursarConfigDict,
} from "../../src/index.js";
import type { BursarRuntimeStartOptions } from "../../src/node.js";
import type { DodoClient } from "../../src/providers/dodo/index.js";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends <Value>() => Value extends Right ? 1 : 2
    ? (<Value>() => Value extends Right ? 1 : 2) extends <Value>() => Value extends Left ? 1 : 2
      ? true
      : false
    : false;

type Assert<Condition extends true> = Condition;

type PublicApiTypeContracts = [
  Assert<
    Equal<BursarRuntimeStartOptions["shouldRetry"], ((cause: unknown) => boolean) | undefined>
  >,
  Assert<Equal<ReturnType<typeof canonicalBursarConfigDict>, BursarConfigData>>,
  Assert<Equal<Parameters<Bursar["ingestBillingEvent"]>[0], BillingEvent>>,
  Assert<DodoPayments extends DodoClient ? true : false>,
];

export type { PublicApiTypeContracts };
