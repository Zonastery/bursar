"""Pure resolver for configurable free-allowance reset windows (WS9).

``resolve_allowance_window`` computes the ``[period_start, period_end)`` window
that a user's plan allowance resets on, given the allowance period mode and an
anchor timestamp (when the user was assigned the plan). All computation is
UTC-only, date granularity (time-of-day is discarded) — there is no local-time
conversion anywhere, so the resolver is unaffected by DST in any zone.

Supported ``period`` values (mirrors ``PlanDefinition.allowance_period``):

- ``"calendar_month"`` (default): resets on the 1st of each UTC calendar month.
  This is the regression-safety baseline — it must match the pre-WS9 SQL
  ``date_trunc('month', now() AT TIME ZONE 'UTC')`` behavior exactly.
- ``"rolling_30d"``: resets every 30 days from the anchor (plan-assignment time).
  Falls back to ``calendar_month`` when no anchor is available.
- ``"anniversary"``: resets monthly on the anchor's day-of-month, clamped to the
  target month's actual length (e.g. an anchor of the 31st resets on the 28th/29th
  in February, then returns to the 31st in a 31-day month). Falls back to
  ``calendar_month`` when no anchor is available.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

__all__ = ["resolve_allowance_window"]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    """Add ``months`` calendar months to ``d``, returning the 1st of that month."""
    total = (d.year * 12 + (d.month - 1)) + months
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, 1)


def _calendar_month_window(now: date) -> tuple[date, date]:
    start = _month_start(now)
    end = _add_months(start, 1)
    return start, end


def _clamped_day(year: int, month: int, day: int) -> date:
    """Return ``date(year, month, day)``, clamping ``day`` to the month's length."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _rolling_30d_window(now: date, anchor_date: date) -> tuple[date, date]:
    elapsed_days = (now - anchor_date).days
    window_index = elapsed_days // 30
    start = anchor_date + timedelta(days=30 * window_index)
    end = start + timedelta(days=30)
    return start, end


def _anniversary_window(now: date, anchor_date: date) -> tuple[date, date]:
    """Monthly window whose reset day-of-month equals ``anchor_date.day``.

    The reset day is clamped to each target month's actual length (e.g. an
    anchor day of 31 resets on the 28th/29th in February, then RETURNS to the
    31st in a 31-day month — clamping is re-evaluated per month, not "sticky").
    """
    anchor_day = anchor_date.day

    # Candidate reset date in `now`'s own month.
    this_month_reset = _clamped_day(now.year, now.month, anchor_day)

    if now >= this_month_reset:
        start = this_month_reset
        end_year, end_month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        end = _clamped_day(end_year, end_month, anchor_day)
    else:
        # Most recent occurrence was in the previous month.
        prev_year, prev_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = _clamped_day(prev_year, prev_month, anchor_day)
        end = this_month_reset

    return start, end


def resolve_allowance_window(now: datetime, period: str, anchor: datetime | None) -> tuple[date, date]:
    """Resolve the ``[period_start, period_end)`` allowance window (UTC, date-only).

    Args:
        now: Current instant. Only the UTC date part is used (time-of-day is
            discarded); callers should pass a UTC-aware (or UTC-naive)
            datetime — this function does not perform any timezone conversion.
        period: One of ``"calendar_month"``, ``"rolling_30d"``, ``"anniversary"``.
        anchor: When the user was assigned the plan (used by ``rolling_30d`` and
            ``anniversary``). ``None`` falls back to ``calendar_month`` behavior
            for those modes.

    Returns:
        ``(period_start, period_end)`` as ``date`` objects, both UTC, with
        ``period_end`` EXCLUSIVE (i.e. the window is ``[period_start, period_end)``).

    Raises:
        ValueError: If ``period`` is not a recognized allowance-period mode.
    """
    now_date = now.date()

    if period == "calendar_month":
        return _calendar_month_window(now_date)

    if period == "rolling_30d":
        if anchor is None:
            return _calendar_month_window(now_date)
        return _rolling_30d_window(now_date, anchor.date())

    if period == "anniversary":
        if anchor is None:
            return _calendar_month_window(now_date)
        return _anniversary_window(now_date, anchor.date())

    raise ValueError(f"unrecognized allowance_period: {period!r}")
