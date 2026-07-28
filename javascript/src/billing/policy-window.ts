import { Temporal } from "@js-temporal/polyfill";

import type { Window } from "../config.js";

const MILLISECONDS_PER_DAY = 86_400_000;
const CALENDAR_ANCHOR = Temporal.PlainDate.from("2000-01-01");
const WEEK_ANCHOR = Temporal.PlainDate.from("2000-01-03");

type AutoRechargeWindow = Extract<Window, { type: "calendar" | "rolling" }>;

export interface ResolvedPolicyWindow {
  unit: "second" | "minute" | "hour" | "day" | "week" | "month" | "year";
  count: number;
  anchor: "calendar" | "rolling";
  timezone: string;
  start: string;
  end: string;
  durationDays: number;
}

function rollingStart(
  end: Temporal.ZonedDateTime,
  unit: Extract<AutoRechargeWindow, { type: "rolling" }>["duration"]["unit"],
  count: number,
): Temporal.ZonedDateTime {
  switch (unit) {
    case "second":
      return end.subtract({ seconds: count });
    case "minute":
      return end.subtract({ minutes: count });
    case "hour":
      return end.subtract({ hours: count });
    case "day":
      return end.subtract({ days: count });
    case "week":
      return end.subtract({ weeks: count });
    default:
      throw new Error("auto_recharge_window_unit_not_supported");
  }
}

function calendarStart(
  now: Temporal.ZonedDateTime,
  unit: Extract<AutoRechargeWindow, { type: "calendar" }>["unit"],
  count: number,
): Temporal.ZonedDateTime {
  if (unit === "day" || unit === "week") {
    const anchor = unit === "week" ? WEEK_ANCHOR : CALENDAR_ANCHOR;
    const stepDays = count * (unit === "week" ? 7 : 1);
    const elapsedDays = anchor.until(now.toPlainDate(), { largestUnit: "day" }).days;
    const startDate = anchor.add({
      days: Math.floor(elapsedDays / stepDays) * stepDays,
    });
    return startDate.toZonedDateTime(now.timeZoneId);
  }

  const stepMonths = count * (unit === "year" ? 12 : 1);
  const currentMonth = now.year * 12 + now.month - 1;
  const anchorMonth = 2000 * 12;
  const startMonth =
    anchorMonth + Math.floor((currentMonth - anchorMonth) / stepMonths) * stepMonths;
  const year = Math.floor(startMonth / 12);
  const month = startMonth - year * 12 + 1;
  return Temporal.PlainDate.from({ year, month, day: 1 }).toZonedDateTime(now.timeZoneId);
}

function calendarEnd(
  start: Temporal.ZonedDateTime,
  unit: Extract<AutoRechargeWindow, { type: "calendar" }>["unit"],
  count: number,
): Temporal.ZonedDateTime {
  switch (unit) {
    case "day":
      return start.add({ days: count });
    case "week":
      return start.add({ weeks: count });
    case "month":
      return start.add({ months: count });
    case "year":
      return start.add({ years: count });
  }
}

/**
 * Resolves the reporting boundary that corresponds to PostgreSQL's
 * `bursar.policy_period_window`.
 *
 * SQL remains authoritative for claiming an auto-recharge attempt. This
 * calculation is used only to report the current window and query its count.
 */
export function resolveAutoRechargeWindow(
  window: AutoRechargeWindow,
  now: Temporal.Instant = Temporal.Now.instant(),
): ResolvedPolicyWindow {
  if (window.type === "rolling") {
    const timezone = "UTC";
    const end = now.toZonedDateTimeISO(timezone);
    const start = rollingStart(end, window.duration.unit, window.duration.count);
    return {
      unit: window.duration.unit,
      count: window.duration.count,
      anchor: "rolling",
      timezone,
      start: start.toInstant().toString(),
      end: end.toInstant().toString(),
      durationDays:
        (end.toInstant().epochMilliseconds - start.toInstant().epochMilliseconds) /
        MILLISECONDS_PER_DAY,
    };
  }

  const zonedNow = now.toZonedDateTimeISO(window.timezone);
  const start = calendarStart(zonedNow, window.unit, window.count);
  const end = calendarEnd(start, window.unit, window.count);
  return {
    unit: window.unit,
    count: window.count,
    anchor: "calendar",
    timezone: window.timezone,
    start: start.toInstant().toString(),
    end: end.toInstant().toString(),
    durationDays:
      (end.toInstant().epochMilliseconds - start.toInstant().epochMilliseconds) /
      MILLISECONDS_PER_DAY,
  };
}
