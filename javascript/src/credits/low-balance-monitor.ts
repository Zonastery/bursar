import { Decimal } from "decimal.js";
import { LRUCache } from "lru-cache";

import type { NormalizedLogger } from "../shared/logger.js";
import type { StructuredObject } from "../shared/json.js";
import type { CreditEvent, CreditEventType } from "./events.js";
import type { DeductionSuccess } from "./types/index.js";
import type { LowBalanceConfig } from "./service-types.js";
import { toDecimal } from "./amount.js";

const DEFAULT_MAX_TRACKED_USERS = 100_000;

type EmitCreditEvent = (type: CreditEventType, userId: string, data?: StructuredObject) => void;

/**
 * Owns edge-trigger state for low-balance and overdraft notifications.
 *
 * Keeping this state outside CreditsService makes the event semantics
 * independently testable and keeps the service focused on orchestration.
 */
export class LowBalanceMonitor {
  private readonly thresholds: Decimal[] | null;
  private readonly handler: ((event: CreditEvent) => void | Promise<void>) | null;
  private readonly breachedByUser: LRUCache<string, Set<string>>;

  constructor(
    config: LowBalanceConfig | null | undefined,
    private readonly emit: EmitCreditEvent,
    private readonly logger: NormalizedLogger,
  ) {
    this.thresholds = config?.thresholds?.length
      ? config.thresholds.map(toDecimal).sort((left, right) => right.comparedTo(left))
      : null;
    this.handler = config?.onTrigger ?? null;
    const maxTrackedUsers = config?.maxTrackedUsers ?? DEFAULT_MAX_TRACKED_USERS;
    if (!Number.isSafeInteger(maxTrackedUsers) || maxTrackedUsers < 1) {
      throw new RangeError("lowBalance.maxTrackedUsers must be a positive safe integer");
    }
    this.breachedByUser = new LRUCache({ max: maxTrackedUsers });
  }

  rearmAfterCredit(userId: string, balance: Decimal): void {
    if (!this.thresholds) return;
    const breached = this.breachedByUser.get(userId) ?? new Set<string>();
    for (const threshold of this.thresholds) {
      if (balance.gt(threshold)) breached.delete(threshold.toString());
    }
    if (breached.size === 0) {
      this.breachedByUser.delete(userId);
    } else {
      this.breachedByUser.set(userId, breached);
    }
  }

  async afterCharge(userId: string, result: DeductionSuccess): Promise<void> {
    if (result.idempotent) return;
    if (result.balanceAfter.lt(0)) {
      this.emit("credits.overdraft", userId, {
        balance: result.balanceAfter,
        amount: result.amount,
      });
    }
    await this.signalCrossing(userId, result.balanceAfter.plus(result.amount), result.balanceAfter);
  }

  async signalCrossing(
    userId: string,
    balanceBefore: Decimal,
    balanceAfter: Decimal,
  ): Promise<void> {
    if (!this.thresholds) {
      const threshold = new Decimal(0);
      if (balanceBefore.gt(threshold) && balanceAfter.lte(threshold)) {
        await this.fire(userId, balanceAfter, threshold);
      }
      return;
    }

    const breached = this.breachedByUser.get(userId) ?? new Set<string>();
    const newlyCrossed: Decimal[] = [];
    for (const threshold of this.thresholds) {
      if (balanceAfter.lte(threshold)) {
        if (!breached.has(threshold.toString())) {
          breached.add(threshold.toString());
          newlyCrossed.push(threshold);
        }
      } else {
        breached.delete(threshold.toString());
      }
    }
    if (breached.size === 0) {
      this.breachedByUser.delete(userId);
    } else {
      this.breachedByUser.set(userId, breached);
    }
    if (newlyCrossed.length > 0) {
      const lowest = newlyCrossed.reduce((minimum, threshold) =>
        threshold.lt(minimum) ? threshold : minimum,
      );
      await this.fire(userId, balanceAfter, lowest);
    }
  }

  private async fire(userId: string, balance: Decimal, threshold: Decimal): Promise<void> {
    const data = { balance, threshold };
    this.emit("credits.low_balance", userId, data);
    if (!this.handler) return;

    try {
      await this.handler({
        type: "credits.low_balance",
        timestamp: new Date(),
        userId,
        data,
      });
    } catch (error) {
      this.logger.error("[CreditsService] onLowBalance handler failed", {
        userId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
}
