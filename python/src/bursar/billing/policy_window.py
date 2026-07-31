"""Exact auto-recharge policy-window resolution shared with the JS SDK."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from bursar.config.types import CalendarWindow, RollingWindow

WindowUnit = Literal["second", "minute", "hour", "day", "week", "month", "year"]


class ResolvedPolicyWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit: WindowUnit
    count: int
    anchor: Literal["calendar", "rolling"]
    timezone: str
    start: str
    end: str
    duration_days: float


_CALENDAR_ANCHOR = date(2000, 1, 1)
_WEEK_ANCHOR = date(2000, 1, 3)


def _add_calendar_period(start: datetime, unit: str, count: int) -> datetime:
    if unit == "day":
        return start + timedelta(days=count)
    if unit == "week":
        return start + timedelta(weeks=count)
    if unit == "month":
        month_index = start.year * 12 + start.month - 1 + count
        return start.replace(year=month_index // 12, month=month_index % 12 + 1)
    if unit == "year":
        return start.replace(year=start.year + count)
    raise ValueError("auto_recharge_window_unit_not_supported")


def _calendar_start(now: datetime, window: CalendarWindow) -> datetime:
    if window.unit in {"day", "week"}:
        anchor = _WEEK_ANCHOR if window.unit == "week" else _CALENDAR_ANCHOR
        step_days = window.count * (7 if window.unit == "week" else 1)
        elapsed_days = (now.date() - anchor).days
        start_date = anchor + timedelta(days=(elapsed_days // step_days) * step_days)
        return datetime.combine(start_date, time.min, tzinfo=now.tzinfo)

    if window.unit == "month":
        current_month = now.year * 12 + now.month - 1
        anchor_month = _CALENDAR_ANCHOR.year * 12 + _CALENDAR_ANCHOR.month - 1
        start_month = anchor_month + ((current_month - anchor_month) // window.count) * window.count
        return datetime(start_month // 12, start_month % 12 + 1, 1, tzinfo=now.tzinfo)

    start_year = _CALENDAR_ANCHOR.year + ((now.year - _CALENDAR_ANCHOR.year) // window.count) * window.count
    return datetime(start_year, 1, 1, tzinfo=now.tzinfo)


def resolve_auto_recharge_window(
    window: CalendarWindow | RollingWindow,
    now: datetime | None = None,
) -> ResolvedPolicyWindow:
    """Resolve the same current policy period as the JS SDK and SQL RPC."""

    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("auto-recharge policy window requires a timezone-aware instant")

    if window.type == "rolling":
        end = instant.astimezone(UTC)
        multipliers = {
            "second": timedelta(seconds=window.duration.count),
            "minute": timedelta(minutes=window.duration.count),
            "hour": timedelta(hours=window.duration.count),
            "day": timedelta(days=window.duration.count),
            "week": timedelta(weeks=window.duration.count),
        }
        duration = multipliers.get(window.duration.unit)
        if duration is None:
            raise ValueError("auto_recharge_window_unit_not_supported")
        start = end - duration
        unit: WindowUnit = window.duration.unit
        anchor: Literal["calendar", "rolling"] = "rolling"
        timezone = "UTC"
    else:
        timezone = window.timezone
        zoned_now = instant.astimezone(ZoneInfo(timezone))
        start = _calendar_start(zoned_now, window)
        end = _add_calendar_period(start, window.unit, window.count)
        unit = window.unit
        anchor = "calendar"

    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    return ResolvedPolicyWindow(
        unit=unit,
        count=window.duration.count if window.type == "rolling" else window.count,
        anchor=anchor,
        timezone=timezone,
        start=start_utc.isoformat(),
        end=end_utc.isoformat(),
        duration_days=(end_utc - start_utc).total_seconds() / 86_400,
    )
