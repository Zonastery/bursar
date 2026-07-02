"""Tests for the free-allowance reset window resolver (WS9a).

All computation is UTC-only, date granularity. These tests verify:
- calendar_month matches the pre-WS9 SQL date_trunc('month', ...) behavior
  exactly (the regression-safety baseline).
- rolling_30d windows advance in fixed 30-day increments from the anchor.
- anniversary windows reset on the anchor's day-of-month, correctly clamping
  in short months and RETURNING to the unclamped day afterward.
- None anchor always falls back to calendar_month for rolling_30d/anniversary.
- The resolver is unaffected by non-UTC local time / DST (UTC-only, no
  local-time conversion anywhere).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ducto.allowance import resolve_allowance_window


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestCalendarMonth:
    """calendar_month: first-of-month to first-of-next-month, UTC."""

    def test_mid_month(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 15, 12, 30), "calendar_month", None)
        assert start == date(2024, 6, 1)
        assert end == date(2024, 7, 1)

    def test_first_instant_of_month(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 1, 0, 0, 0), "calendar_month", None)
        assert start == date(2024, 6, 1)
        assert end == date(2024, 7, 1)

    def test_last_instant_of_month(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 30, 23, 59, 59), "calendar_month", None)
        assert start == date(2024, 6, 1)
        assert end == date(2024, 7, 1)

    def test_december_rolls_into_january_next_year(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 12, 15), "calendar_month", None)
        assert start == date(2024, 12, 1)
        assert end == date(2025, 1, 1)

    def test_february_leap_year(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 2, 20), "calendar_month", None)
        assert start == date(2024, 2, 1)
        assert end == date(2024, 3, 1)

    def test_anchor_is_ignored_for_calendar_month(self) -> None:
        # calendar_month never depends on the anchor.
        anchor = _utc(2020, 1, 15)
        start, end = resolve_allowance_window(_utc(2024, 6, 15), "calendar_month", anchor)
        assert start == date(2024, 6, 1)
        assert end == date(2024, 7, 1)


class TestRolling30d:
    """rolling_30d: fixed 30-day windows from the anchor date."""

    def test_anchor_none_falls_back_to_calendar_month(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 15), "rolling_30d", None)
        assert start == date(2024, 6, 1)
        assert end == date(2024, 7, 1)

    def test_zero_days_elapsed(self) -> None:
        anchor = _utc(2024, 1, 1)
        start, end = resolve_allowance_window(_utc(2024, 1, 1), "rolling_30d", anchor)
        assert start == date(2024, 1, 1)
        assert end == date(2024, 1, 31)

    def test_twenty_nine_days_elapsed_still_in_first_window(self) -> None:
        anchor = _utc(2024, 1, 1)
        start, end = resolve_allowance_window(_utc(2024, 1, 30), "rolling_30d", anchor)  # +29 days
        assert start == date(2024, 1, 1)
        assert end == date(2024, 1, 31)

    def test_thirty_days_elapsed_rolls_to_next_window(self) -> None:
        anchor = _utc(2024, 1, 1)
        start, end = resolve_allowance_window(_utc(2024, 1, 31), "rolling_30d", anchor)  # +30 days
        assert start == date(2024, 1, 31)
        assert end == date(2024, 3, 1)  # Jan 31 + 30 days

    def test_thirty_one_days_elapsed_still_in_second_window(self) -> None:
        anchor = _utc(2024, 1, 1)
        start, end = resolve_allowance_window(_utc(2024, 2, 1), "rolling_30d", anchor)  # +31 days
        assert start == date(2024, 1, 31)
        assert end == date(2024, 3, 1)

    def test_sixty_days_elapsed_rolls_to_third_window(self) -> None:
        anchor = _utc(2024, 1, 1)
        start, end = resolve_allowance_window(_utc(2024, 3, 1), "rolling_30d", anchor)  # +60 days
        assert start == date(2024, 3, 1)
        assert end == date(2024, 3, 31)

    def test_anchor_time_of_day_is_discarded(self) -> None:
        # Anchor at 23:59 should behave the same as anchor at 00:00 (date-only).
        anchor = _utc(2024, 1, 1, 23, 59, 59)
        start, end = resolve_allowance_window(_utc(2024, 1, 1, 0, 0, 0), "rolling_30d", anchor)
        assert start == date(2024, 1, 1)
        assert end == date(2024, 1, 31)


class TestAnniversary:
    """anniversary: monthly reset on the anchor's day-of-month, clamped."""

    def test_anchor_none_falls_back_to_calendar_month(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 15), "anniversary", None)
        assert start == date(2024, 6, 1)
        assert end == date(2024, 7, 1)

    def test_day_15_resets_on_the_15th_each_month(self) -> None:
        anchor = _utc(2024, 1, 15)
        start, end = resolve_allowance_window(_utc(2024, 6, 20), "anniversary", anchor)
        assert start == date(2024, 6, 15)
        assert end == date(2024, 7, 15)

    def test_day_15_before_reset_day_uses_previous_month(self) -> None:
        anchor = _utc(2024, 1, 15)
        start, end = resolve_allowance_window(_utc(2024, 6, 10), "anniversary", anchor)
        assert start == date(2024, 5, 15)
        assert end == date(2024, 6, 15)

    def test_day_15_exactly_on_reset_day(self) -> None:
        anchor = _utc(2024, 1, 15)
        start, end = resolve_allowance_window(_utc(2024, 6, 15), "anniversary", anchor)
        assert start == date(2024, 6, 15)
        assert end == date(2024, 7, 15)

    def test_day_31_clamps_to_28_in_non_leap_february(self) -> None:
        anchor = _utc(2023, 1, 31)
        start, end = resolve_allowance_window(_utc(2023, 2, 20), "anniversary", anchor)
        assert start == date(2023, 1, 31)
        assert end == date(2023, 2, 28)  # 2023 is not a leap year

    def test_day_31_clamps_to_29_in_leap_february(self) -> None:
        anchor = _utc(2024, 1, 31)
        start, end = resolve_allowance_window(_utc(2024, 2, 20), "anniversary", anchor)
        assert start == date(2024, 1, 31)
        assert end == date(2024, 2, 29)  # 2024 is a leap year

    def test_day_31_returns_to_31_in_march_non_leap_year(self) -> None:
        """The trickiest case: Feb clamps to 28, but March 31 must NOT stay clamped."""
        anchor = _utc(2023, 1, 31)
        start, end = resolve_allowance_window(_utc(2023, 3, 15), "anniversary", anchor)
        assert start == date(2023, 2, 28)  # most recent occurrence (clamped)
        assert end == date(2023, 3, 31)  # next occurrence RETURNS to 31 (not sticky)

    def test_day_31_returns_to_31_in_march_leap_year(self) -> None:
        anchor = _utc(2024, 1, 31)
        start, end = resolve_allowance_window(_utc(2024, 3, 15), "anniversary", anchor)
        assert start == date(2024, 2, 29)  # most recent occurrence (clamped, leap)
        assert end == date(2024, 3, 31)  # next occurrence RETURNS to 31

    def test_day_31_window_after_march_reset(self) -> None:
        """Once past the March 31 reset, the window is [Mar 31, Apr 30)."""
        anchor = _utc(2023, 1, 31)
        start, end = resolve_allowance_window(_utc(2023, 4, 5), "anniversary", anchor)
        assert start == date(2023, 3, 31)
        assert end == date(2023, 4, 30)  # April has 30 days -> clamped

    def test_day_30_clamps_to_28_or_29_in_february(self) -> None:
        anchor = _utc(2024, 1, 30)
        start, end = resolve_allowance_window(_utc(2024, 2, 20), "anniversary", anchor)
        assert start == date(2024, 1, 30)
        assert end == date(2024, 2, 29)  # leap year: clamps to 29, not 28

    def test_day_30_non_leap_year_clamps_to_28(self) -> None:
        anchor = _utc(2023, 1, 30)
        start, end = resolve_allowance_window(_utc(2023, 2, 20), "anniversary", anchor)
        assert start == date(2023, 1, 30)
        assert end == date(2023, 2, 28)

    def test_anniversary_across_year_boundary(self) -> None:
        anchor = _utc(2023, 6, 10)
        start, end = resolve_allowance_window(_utc(2024, 1, 5), "anniversary", anchor)
        assert start == date(2023, 12, 10)
        assert end == date(2024, 1, 10)

    def test_anchor_time_of_day_is_discarded(self) -> None:
        anchor = _utc(2024, 1, 15, 8, 30, 0)
        start, end = resolve_allowance_window(_utc(2024, 6, 20, 23, 0, 0), "anniversary", anchor)
        assert start == date(2024, 6, 15)
        assert end == date(2024, 7, 15)


class TestUtcOnlyNoLocalTimeConversion:
    """The resolver must be UTC-only and unaffected by any local-time/DST zone.

    We only ever pass UTC datetimes; there is no local-time conversion inside
    the resolver at all, so a `now` near a DST transition elsewhere in the
    world cannot perturb the result -- confirmed by using instants that fall
    inside a US/EU DST transition window, expressed in UTC.
    """

    def test_dst_transition_instant_calendar_month(self) -> None:
        # 2024-03-10 07:30 UTC is inside the US DST "spring forward" transition
        # (2am local -> 3am local in US/Eastern, i.e. 06:00-07:00 UTC-ish); using
        # a UTC datetime directly must be unaffected by any such local rule.
        start, end = resolve_allowance_window(_utc(2024, 3, 10, 7, 30), "calendar_month", None)
        assert start == date(2024, 3, 1)
        assert end == date(2024, 4, 1)

    def test_dst_transition_instant_rolling_30d(self) -> None:
        anchor = _utc(2024, 2, 9, 7, 30)
        start, end = resolve_allowance_window(_utc(2024, 3, 10, 7, 30), "rolling_30d", anchor)
        # elapsed = 30 days exactly -> window_index=1 -> start = anchor + 30d
        assert start == date(2024, 3, 10)
        assert end == date(2024, 4, 9)

    def test_dst_transition_instant_anniversary(self) -> None:
        anchor = _utc(2024, 1, 10, 7, 30)
        start, end = resolve_allowance_window(_utc(2024, 3, 10, 7, 30), "anniversary", anchor)
        assert start == date(2024, 3, 10)
        assert end == date(2024, 4, 10)


class TestInvalidPeriod:
    def test_unrecognized_period_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unrecognized allowance_period"):
            resolve_allowance_window(_utc(2024, 1, 1), "weekly", None)


class TestReturnTypesAreDates:
    def test_returns_date_not_datetime(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 15), "calendar_month", None)
        assert type(start) is date
        assert type(end) is date

    def test_end_is_exclusive_one_day_after_last_valid_day(self) -> None:
        start, end = resolve_allowance_window(_utc(2024, 6, 15), "calendar_month", None)
        last_valid_day = end - timedelta(days=1)
        assert last_valid_day == date(2024, 6, 30)
