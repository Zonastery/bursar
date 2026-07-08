/**
 * Configurable free-allowance reset window resolver (WS9a).
 *
 * Pure, UTC-only, DATE-granularity (time-of-day is discarded) window resolver.
 * MUST exactly match the Python resolver: this is the cross-language parity
 * baseline for `allowancePeriod` handling. All computation uses UTC methods
 * exclusively (`getUTCFullYear`, `Date.UTC`, ...) — never local-timezone
 * Date methods.
 *
 * `resolveCalendarWindow` is a separate, anchor-free resolver used by
 * per-feature invocation-count limits (`PlanDefinition.featureLimits`). It
 * supports `"daily"`/`"weekly"`/`"monthly"`/`"yearly"` cadences, all
 * calendar-aligned (every user resets at the same instant) rather than
 * anchored to a per-user timestamp — MUST exactly match
 * `bursar.allowance.resolve_calendar_window` in the Python SDK.
 */

/** Supported free-allowance reset window modes. */
export type AllowancePeriod = "calendar_month" | "rolling_30d" | "anniversary";

/** Supported per-feature invocation-count limit cadences. */
export type FeatureLimitPeriod = "daily" | "weekly" | "monthly" | "yearly";

const MS_PER_DAY = 86_400_000;

/** Truncate a `Date` to UTC midnight (DATE granularity, discards time-of-day). */
function utcMidnight(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

/** Number of days in the given UTC year/month (0-indexed month, like `Date.UTC`). */
function daysInUtcMonth(year: number, month: number): number {
  // Day 0 of "next month" is the last day of `month`.
  return new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
}

/**
 * The `day`-th day of the UTC month `(year, month)`, clamped to that month's
 * actual length (e.g. day 31 in a 28-day February clamps to the 28th).
 */
function clampedDayInMonth(year: number, month: number, day: number): Date {
  const lastDay = daysInUtcMonth(year, month);
  return new Date(Date.UTC(year, month, Math.min(day, lastDay)));
}

/** First day of the UTC month containing `d`, at midnight UTC. */
function calendarMonthStart(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
}

function resolveCalendarMonth(now: Date): { start: Date; end: Date } {
  const start = calendarMonthStart(now);
  const end = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 1));
  return { start, end };
}

function resolveRolling30d(now: Date, anchor: Date | null): { start: Date; end: Date } {
  if (anchor === null) return resolveCalendarMonth(now);
  const anchorMidnight = utcMidnight(anchor);
  const nowMidnight = utcMidnight(now);
  const elapsedDays = Math.floor((nowMidnight.getTime() - anchorMidnight.getTime()) / MS_PER_DAY);
  const windowIndex = Math.floor(elapsedDays / 30);
  const start = new Date(anchorMidnight.getTime() + windowIndex * 30 * MS_PER_DAY);
  const end = new Date(start.getTime() + 30 * MS_PER_DAY);
  return { start, end };
}

function resolveAnniversary(now: Date, anchor: Date | null): { start: Date; end: Date } {
  if (anchor === null) return resolveCalendarMonth(now);
  const anchorMidnight = utcMidnight(anchor);
  const nowMidnight = utcMidnight(now);
  const resetDay = anchorMidnight.getUTCDate();

  // Candidate reset date in the same UTC year/month as `now`, clamped.
  const candidate = clampedDayInMonth(
    nowMidnight.getUTCFullYear(),
    nowMidnight.getUTCMonth(),
    resetDay,
  );

  let start: Date;
  if (candidate.getTime() <= nowMidnight.getTime()) {
    start = candidate;
  } else {
    // The clamped reset date this month is still ahead of `now` — the current
    // window started last month.
    start = clampedDayInMonth(
      nowMidnight.getUTCFullYear(),
      nowMidnight.getUTCMonth() - 1,
      resetDay,
    );
  }

  const end = clampedDayInMonth(start.getUTCFullYear(), start.getUTCMonth() + 1, resetDay);
  return { start, end };
}

/**
 * Resolve the free-allowance reset window containing `now` for the given
 * `period` mode, anchored (where applicable) at `anchor` (typically the
 * plan-assignment timestamp).
 *
 * `start`/`end` are UTC-midnight `Date`s at DATE granularity; `end` is
 * EXCLUSIVE (the window is `[start, end)`).
 */
export function resolveAllowanceWindow(
  now: Date,
  period: AllowancePeriod,
  anchor: Date | null,
): { start: Date; end: Date } {
  switch (period) {
    case "calendar_month":
      return resolveCalendarMonth(now);
    case "rolling_30d":
      return resolveRolling30d(now, anchor);
    case "anniversary":
      return resolveAnniversary(now, anchor);
    default:
      throw new Error(`unrecognized allowance period: ${String(period)}`);
  }
}

/** `[now, now + 1 day)` at UTC midnight. */
function dayWindow(now: Date): { start: Date; end: Date } {
  const start = utcMidnight(now);
  return { start, end: new Date(start.getTime() + MS_PER_DAY) };
}

/** Monday-start ISO week window: `[Monday, next Monday)`. */
function isoWeekWindow(now: Date): { start: Date; end: Date } {
  const midnight = utcMidnight(now);
  // getUTCDay(): 0=Sunday..6=Saturday. Days since the most recent Monday.
  const dayOfWeek = midnight.getUTCDay();
  const daysSinceMonday = (dayOfWeek + 6) % 7;
  const start = new Date(midnight.getTime() - daysSinceMonday * MS_PER_DAY);
  return { start, end: new Date(start.getTime() + 7 * MS_PER_DAY) };
}

/** `[Jan 1, next Jan 1)` of `now`'s UTC year. */
function yearWindow(now: Date): { start: Date; end: Date } {
  const start = new Date(Date.UTC(now.getUTCFullYear(), 0, 1));
  const end = new Date(Date.UTC(now.getUTCFullYear() + 1, 0, 1));
  return { start, end };
}

/**
 * Resolve the calendar-aligned `[start, end)` window for a per-feature
 * invocation-count limit cadence (`FeatureLimit.period`).
 *
 * Unlike {@link resolveAllowanceWindow}, this resolver takes no anchor: every
 * user resets at the same UTC instant. `start`/`end` are UTC-midnight `Date`s
 * at DATE granularity; `end` is EXCLUSIVE (the window is `[start, end)`).
 *
 * MUST exactly match `bursar.allowance.resolve_calendar_window` in Python
 * (same Monday-start ISO week, same year boundary).
 */
export function resolveCalendarWindow(
  now: Date,
  period: FeatureLimitPeriod,
): { start: Date; end: Date } {
  switch (period) {
    case "daily":
      return dayWindow(now);
    case "weekly":
      return isoWeekWindow(now);
    case "monthly":
      return resolveCalendarMonth(now);
    case "yearly":
      return yearWindow(now);
    default:
      throw new Error(`unrecognized feature-limit period: ${String(period)}`);
  }
}
