import type Decimal from "decimal.js";

import type { Logger } from "../shared/logger.js";
import type {
  AddCreditsResult,
  RevokeCreditsResult,
  SetUserPlanResult,
  UnsetUserPlanResult,
} from "../credits/types/index.js";
import type { BillingEventHandler, BillingEventType } from "./types/index.js";

export type ResolveUser = (
  provider: string,
  providerCustomerId: string | null,
  email: string | null,
) => string | null;

export interface BillingServiceOptions {
  provisioning?: BillingProvisioningPort | null;
  resolveUser?: ResolveUser | null;
  eventHandlers?: Partial<Record<BillingEventType, BillingEventHandler>>;
  autoSelectEntitlementSource?: boolean;
  /** Access grace after a failed subscription payment. Defaults to seven days. */
  pastDueGracePeriodMs?: number;
  /** Plan assigned when a paid subscription reaches terminal state. */
  terminalPlanKey?: string | null;
  logger?: Logger | null;
}

/**
 * The narrow credit capability billing may request during subscription
 * provisioning. Billing does not depend on the full credit service.
 */
export interface BillingProvisioningPort {
  getUserPlan?(userId: string): Promise<{ planAssignedAt?: Date | string | null } | null>;
  setUserPlan(
    userId: string,
    planKey: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult>;
  unsetUserPlan(userId: string): Promise<UnsetUserPlanResult>;
  addCredits(
    userId: string,
    amount: Decimal | number,
    options?: {
      type?: string;
      bucket?: string | null;
      idempotencyKey?: string | null;
    },
  ): Promise<AddCreditsResult>;
  deductCredits(
    userId: string,
    amount: Decimal | number,
    options?: { entryType?: string; bucket?: string | null },
  ): Promise<AddCreditsResult>;
  revokeCreditsByEntryType(userId: string, entryType: string): Promise<RevokeCreditsResult>;
}
