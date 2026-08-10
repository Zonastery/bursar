import type { Logger } from "../shared/logger.js";
import type { SetUserPlanResult, UnsetUserPlanResult } from "../credits/types/index.js";
import type { BillingEventHandler, BillingEventType } from "./types/index.js";

export interface BillingServiceOptions {
  provisioning?: BillingProvisioningPort | null;
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
  getUserPlan(userId: string): Promise<{ planAssignedAt?: Date | string | null } | null>;
  setUserPlan(
    userId: string,
    planKey: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult>;
  unsetUserPlan(userId: string): Promise<UnsetUserPlanResult>;
}
