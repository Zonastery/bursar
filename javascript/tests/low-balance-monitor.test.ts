import { Decimal } from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { LowBalanceMonitor } from "../src/credits/low-balance-monitor.js";
import { noopLogger } from "../src/shared/logger.js";

describe("LowBalanceMonitor", () => {
  it("bounds retained breach state with the configured LRU capacity", async () => {
    const emit = vi.fn();
    const monitor = new LowBalanceMonitor(
      { thresholds: ["10"], maxTrackedUsers: 2 },
      emit,
      noopLogger,
    );

    await monitor.signalCrossing("user-1", new Decimal(11), new Decimal(9));
    await monitor.signalCrossing("user-2", new Decimal(11), new Decimal(9));
    await monitor.signalCrossing("user-3", new Decimal(11), new Decimal(9));
    await monitor.signalCrossing("user-1", new Decimal(9), new Decimal(8));

    expect(emit).toHaveBeenCalledTimes(4);
  });

  it("rejects invalid cache capacities at construction", () => {
    expect(
      () => new LowBalanceMonitor({ thresholds: ["10"], maxTrackedUsers: 0 }, vi.fn(), noopLogger),
    ).toThrow(/positive safe integer/);
  });
});
