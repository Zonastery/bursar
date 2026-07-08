import { describe, it, expect } from "vitest";
import { resolveAllowanceWindow, resolveCalendarWindow } from "../src/allowance.js";

/** Build a UTC-midnight Date from y/m/d (m is 1-indexed for readability). */
function utc(y: number, m: number, d: number): Date {
  return new Date(Date.UTC(y, m - 1, d));
}

function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

describe("resolveAllowanceWindow", () => {
  describe("calendar_month", () => {
    it("mid-month resolves to the containing UTC month", () => {
      const { start, end } = resolveAllowanceWindow(utc(2026, 3, 15), "calendar_month", null);
      expect(iso(start)).toBe("2026-03-01");
      expect(iso(end)).toBe("2026-04-01");
    });

    it("exact start boundary resolves to the same month", () => {
      const { start, end } = resolveAllowanceWindow(utc(2026, 3, 1), "calendar_month", null);
      expect(iso(start)).toBe("2026-03-01");
      expect(iso(end)).toBe("2026-04-01");
    });

    it("last day of month still resolves to that month (end exclusive)", () => {
      const { start, end } = resolveAllowanceWindow(utc(2026, 3, 31), "calendar_month", null);
      expect(iso(start)).toBe("2026-03-01");
      expect(iso(end)).toBe("2026-04-01");
    });

    it("December rolls over to January of the next year", () => {
      const { start, end } = resolveAllowanceWindow(utc(2026, 12, 15), "calendar_month", null);
      expect(iso(start)).toBe("2026-12-01");
      expect(iso(end)).toBe("2027-01-01");
    });

    it("ignores anchor entirely", () => {
      const { start, end } = resolveAllowanceWindow(
        utc(2026, 3, 15),
        "calendar_month",
        utc(2025, 6, 10),
      );
      expect(iso(start)).toBe("2026-03-01");
      expect(iso(end)).toBe("2026-04-01");
    });
  });

  describe("rolling_30d", () => {
    const anchor = utc(2026, 1, 1);

    it("falls back to calendar_month when anchor is null", () => {
      const now = utc(2026, 3, 15);
      const rolling = resolveAllowanceWindow(now, "rolling_30d", null);
      const calendar = resolveAllowanceWindow(now, "calendar_month", null);
      expect(iso(rolling.start)).toBe(iso(calendar.start));
      expect(iso(rolling.end)).toBe(iso(calendar.end));
    });

    it("day 0 (== anchor): first window", () => {
      const { start, end } = resolveAllowanceWindow(anchor, "rolling_30d", anchor);
      expect(iso(start)).toBe("2026-01-01");
      expect(iso(end)).toBe("2026-01-31");
    });

    it("day 29: still within the first 30-day window", () => {
      const now = new Date(anchor.getTime() + 29 * 86_400_000);
      const { start, end } = resolveAllowanceWindow(now, "rolling_30d", anchor);
      expect(iso(start)).toBe("2026-01-01");
      expect(iso(end)).toBe("2026-01-31");
    });

    it("day 30: rolls into the second window", () => {
      const now = new Date(anchor.getTime() + 30 * 86_400_000);
      const { start, end } = resolveAllowanceWindow(now, "rolling_30d", anchor);
      expect(iso(start)).toBe("2026-01-31");
      expect(iso(end)).toBe("2026-03-02");
    });

    it("day 31: still within the second window", () => {
      const now = new Date(anchor.getTime() + 31 * 86_400_000);
      const { start, end } = resolveAllowanceWindow(now, "rolling_30d", anchor);
      expect(iso(start)).toBe("2026-01-31");
      expect(iso(end)).toBe("2026-03-02");
    });

    it("day 60: rolls into the third window", () => {
      const now = new Date(anchor.getTime() + 60 * 86_400_000);
      const { start, end } = resolveAllowanceWindow(now, "rolling_30d", anchor);
      expect(iso(start)).toBe("2026-03-02");
      expect(iso(end)).toBe("2026-04-01");
    });
  });

  describe("anniversary", () => {
    it("falls back to calendar_month when anchor is null", () => {
      const now = utc(2026, 3, 15);
      const anniv = resolveAllowanceWindow(now, "anniversary", null);
      const calendar = resolveAllowanceWindow(now, "calendar_month", null);
      expect(iso(anniv.start)).toBe(iso(calendar.start));
      expect(iso(anniv.end)).toBe(iso(calendar.end));
    });

    it("anchor day 15: mid-month window", () => {
      const anchor = utc(2026, 1, 15);
      const { start, end } = resolveAllowanceWindow(utc(2026, 3, 20), "anniversary", anchor);
      expect(iso(start)).toBe("2026-03-15");
      expect(iso(end)).toBe("2026-04-15");
    });

    it("anchor day 15: just before the reset day is still the previous window", () => {
      const anchor = utc(2026, 1, 15);
      const { start, end } = resolveAllowanceWindow(utc(2026, 3, 14), "anniversary", anchor);
      expect(iso(start)).toBe("2026-02-15");
      expect(iso(end)).toBe("2026-03-15");
    });

    it("anchor day 31, non-leap year: February clamps to 28, March returns to 31", () => {
      const anchor = utc(2026, 1, 31); // 2026 is not a leap year
      const feb = resolveAllowanceWindow(utc(2026, 2, 15), "anniversary", anchor);
      expect(iso(feb.start)).toBe("2026-01-31");
      expect(iso(feb.end)).toBe("2026-02-28");

      const mar = resolveAllowanceWindow(utc(2026, 3, 1), "anniversary", anchor);
      expect(iso(mar.start)).toBe("2026-02-28");
      expect(iso(mar.end)).toBe("2026-03-31");

      const apr = resolveAllowanceWindow(utc(2026, 4, 1), "anniversary", anchor);
      expect(iso(apr.start)).toBe("2026-03-31");
      expect(iso(apr.end)).toBe("2026-04-30");
    });

    it("anchor day 31, leap year: February clamps to 29, March returns to 31", () => {
      const anchor = utc(2028, 1, 31); // 2028 is a leap year
      const feb = resolveAllowanceWindow(utc(2028, 2, 15), "anniversary", anchor);
      expect(iso(feb.start)).toBe("2028-01-31");
      expect(iso(feb.end)).toBe("2028-02-29");

      const mar = resolveAllowanceWindow(utc(2028, 3, 1), "anniversary", anchor);
      expect(iso(mar.start)).toBe("2028-02-29");
      expect(iso(mar.end)).toBe("2028-03-31");
    });

    it("anchor day 30: February clamps to 28 (non-leap)", () => {
      const anchor = utc(2026, 1, 30);
      const feb = resolveAllowanceWindow(utc(2026, 2, 15), "anniversary", anchor);
      expect(iso(feb.start)).toBe("2026-01-30");
      expect(iso(feb.end)).toBe("2026-02-28");

      const mar = resolveAllowanceWindow(utc(2026, 3, 1), "anniversary", anchor);
      expect(iso(mar.start)).toBe("2026-02-28");
      expect(iso(mar.end)).toBe("2026-03-30");
    });
  });

  describe("UTC-only (no local-timezone dependence)", () => {
    it("uses UTC boundaries regardless of local machine timezone", () => {
      // A Date constructed at UTC midnight must report that same UTC date,
      // independent of TZ env — asserted via the UTC getters exclusively.
      const now = utc(2026, 6, 1);
      const { start, end } = resolveAllowanceWindow(now, "calendar_month", null);
      expect(start.getUTCHours()).toBe(0);
      expect(end.getUTCHours()).toBe(0);
      expect(iso(start)).toBe("2026-06-01");
      expect(iso(end)).toBe("2026-07-01");
    });
  });

  it("throws for an unrecognized period", () => {
    expect(() =>
      resolveAllowanceWindow(utc(2026, 1, 1), "bogus" as never, null),
    ).toThrow();
  });
});

describe("resolveCalendarWindow (per-feature invocation-count limits)", () => {
  describe("daily", () => {
    it("resolves to [UTC midnight, next UTC midnight)", () => {
      const { start, end } = resolveCalendarWindow(
        new Date(Date.UTC(2026, 2, 15, 13, 45, 0)),
        "daily",
      );
      expect(iso(start)).toBe("2026-03-15");
      expect(iso(end)).toBe("2026-03-16");
    });
  });

  describe("weekly (Monday-start ISO week)", () => {
    it("mid-week resolves to the containing Monday-start week", () => {
      // 2026-03-18 is a Wednesday.
      const { start, end } = resolveCalendarWindow(utc(2026, 3, 18), "weekly");
      expect(iso(start)).toBe("2026-03-16"); // Monday
      expect(iso(end)).toBe("2026-03-23"); // next Monday
    });

    it("exact Monday boundary resolves to itself as the start", () => {
      const { start, end } = resolveCalendarWindow(utc(2026, 3, 16), "weekly");
      expect(iso(start)).toBe("2026-03-16");
      expect(iso(end)).toBe("2026-03-23");
    });

    it("Sunday resolves to the PRECEDING Monday (end of that week)", () => {
      // 2026-03-22 is a Sunday, still within the week that started Monday 03-16.
      const { start, end } = resolveCalendarWindow(utc(2026, 3, 22), "weekly");
      expect(iso(start)).toBe("2026-03-16");
      expect(iso(end)).toBe("2026-03-23");
    });

    it("week spanning a month boundary", () => {
      // 2026-03-30 is a Monday; the week runs into April.
      const { start, end } = resolveCalendarWindow(utc(2026, 3, 31), "weekly");
      expect(iso(start)).toBe("2026-03-30");
      expect(iso(end)).toBe("2026-04-06");
    });
  });

  describe("monthly", () => {
    it("matches resolveAllowanceWindow's calendar_month for the same instant", () => {
      const now = utc(2026, 3, 15);
      const monthly = resolveCalendarWindow(now, "monthly");
      const calendarMonth = resolveAllowanceWindow(now, "calendar_month", null);
      expect(iso(monthly.start)).toBe(iso(calendarMonth.start));
      expect(iso(monthly.end)).toBe(iso(calendarMonth.end));
    });

    it("December rolls over to January of the next year", () => {
      const { start, end } = resolveCalendarWindow(utc(2026, 12, 15), "monthly");
      expect(iso(start)).toBe("2026-12-01");
      expect(iso(end)).toBe("2027-01-01");
    });
  });

  describe("yearly", () => {
    it("resolves to [Jan 1, next Jan 1)", () => {
      const { start, end } = resolveCalendarWindow(utc(2026, 6, 15), "yearly");
      expect(iso(start)).toBe("2026-01-01");
      expect(iso(end)).toBe("2027-01-01");
    });

    it("Dec 31 -> Jan 1 year boundary: still resolves to the OLD year's window", () => {
      const { start, end } = resolveCalendarWindow(utc(2026, 12, 31), "yearly");
      expect(iso(start)).toBe("2026-01-01");
      expect(iso(end)).toBe("2027-01-01");
    });

    it("Jan 1 resolves to the NEW year's window", () => {
      const { start, end } = resolveCalendarWindow(utc(2027, 1, 1), "yearly");
      expect(iso(start)).toBe("2027-01-01");
      expect(iso(end)).toBe("2028-01-01");
    });
  });

  it("is idempotent when re-resolved from its own (already-aligned) start", () => {
    // The stores derive a feature-limit window's END by re-resolving the
    // window from its already-aligned START — this must be a no-op.
    for (const period of ["daily", "weekly", "monthly", "yearly"] as const) {
      const { start, end } = resolveCalendarWindow(utc(2026, 3, 18), period);
      const reResolved = resolveCalendarWindow(start, period);
      expect(iso(reResolved.start)).toBe(iso(start));
      expect(iso(reResolved.end)).toBe(iso(end));
    }
  });

  it("throws for an unrecognized period", () => {
    expect(() => resolveCalendarWindow(utc(2026, 1, 1), "bogus" as never)).toThrow();
  });
});
