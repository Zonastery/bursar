import { Temporal } from "@js-temporal/polyfill";
import { describe, expect, it } from "vitest";

import { resolveAutoRechargeWindow } from "../src/billing/policy-window.js";

describe("auto-recharge policy windows", () => {
  it("uses the actual number of days in a leap-year calendar month", () => {
    const window = resolveAutoRechargeWindow(
      { type: "calendar", unit: "month", count: 1, timezone: "UTC" },
      Temporal.Instant.from("2024-02-15T12:00:00Z"),
    );

    expect(window.start).toBe("2024-02-01T00:00:00Z");
    expect(window.end).toBe("2024-03-01T00:00:00Z");
    expect(window.durationDays).toBe(29);
  });

  it("preserves timezone-aware boundaries across daylight-saving changes", () => {
    const window = resolveAutoRechargeWindow(
      {
        type: "calendar",
        unit: "month",
        count: 1,
        timezone: "America/New_York",
      },
      Temporal.Instant.from("2024-03-15T12:00:00Z"),
    );

    expect(window.start).toBe("2024-03-01T05:00:00Z");
    expect(window.end).toBe("2024-04-01T04:00:00Z");
    expect(window.durationDays).toBeCloseTo(30 + 23 / 24);
  });

  it("anchors multi-week calendar windows to Monday 2000-01-03 like SQL", () => {
    const window = resolveAutoRechargeWindow(
      { type: "calendar", unit: "week", count: 2, timezone: "UTC" },
      Temporal.Instant.from("2024-01-10T12:00:00Z"),
    );

    expect(window.start).toBe("2024-01-01T00:00:00Z");
    expect(window.end).toBe("2024-01-15T00:00:00Z");
    expect(window.durationDays).toBe(14);
  });

  it("supports rolling units without converting them to approximate days", () => {
    const window = resolveAutoRechargeWindow(
      { type: "rolling", duration: { unit: "hour", count: 36 } },
      Temporal.Instant.from("2024-01-10T12:00:00Z"),
    );

    expect(window.start).toBe("2024-01-09T00:00:00Z");
    expect(window.end).toBe("2024-01-10T12:00:00Z");
    expect(window.durationDays).toBe(1.5);
  });
});
