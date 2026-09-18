"""Business day calendars, with SIFMA's US recommendation implemented.

The calendar is data pretending to be code, which is why the holiday rules are
written out one function per holiday rather than compressed: a mis-entered rule
produces a curve that is wrong by a day's accrual on some nodes and right on
others, and that is far easier to review as twelve named rules than as a table.

**The manual check.** ``SIFMA_US`` encodes the twelve recommended US holidays
and the observance rule used for the government bond market — Saturday
observed on the preceding Friday, Sunday on the following Monday. That
observance rule, and nothing else here, is what PRD-001 AC-1.2 marks
``[manual-check]``: it must be confirmed once against SIFMA's published
calendar. Until it is, the fixture generated from these rules carries
``data_quality="assumed"``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol, runtime_checkable

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.errors import UnsupportedConventionError

__all__ = [
    "BusinessDayConvention",
    "Calendar",
    "SIFMAUSCalendar",
    "SIFMA_US",
    "easter_sunday",
]


from enum import Enum


class BusinessDayConvention(Enum):
    """How a date that is not a business day is rolled.

    ``FOLLOWING``
        Next business day, even into the next month.
    ``MODIFIED_FOLLOWING``
        Next business day unless that crosses into a new month, then the
        previous one. The market default for swap schedules.
    ``PRECEDING``
        Previous business day.
    """

    FOLLOWING = "following"
    MODIFIED_FOLLOWING = "modified_following"
    PRECEDING = "preceding"


@runtime_checkable
class Calendar(Protocol):
    """What the rest of the package needs from a calendar."""

    name: str

    def is_business_day(self, day: date) -> bool:
        """True when ``day`` is a settlement day in this calendar."""
        ...


def easter_sunday(year: int) -> date:
    """Easter Sunday in the Gregorian calendar, by the anonymous algorithm.

    Good Friday — the one SIFMA holiday with no fixed rule — is two days
    earlier. Pure arithmetic, so it stays deterministic across platforms.

    Args:
        year: Gregorian year.

    Returns:
        The date of Easter Sunday.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` of a month, with Monday as 0."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` of a month, with Monday as 0."""
    day = date(year, month, 28)
    while True:
        nxt = day + timedelta(days=7)
        if nxt.month != month:
            break
        day = nxt
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Apply the government-bond observance rule to a fixed-date holiday.

    Saturday moves back to Friday, Sunday forward to Monday. This is the line
    AC-1.2 marks for manual confirmation against SIFMA's published list.
    """
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


class SIFMAUSCalendar:
    """SIFMA's recommended US holiday schedule for the fixed income market.

    Twelve holidays. Juneteenth only from 2022, the year it became a federal
    holiday; asking about 2021 correctly reports 19 June as a business day.
    """

    name = "SIFMA_US"

    def holidays(self, year: int) -> frozenset[date]:
        """Every holiday observed in ``year``, as observed dates.

        Args:
            year: Calendar year.

        Returns:
            The observed dates, already rolled off weekends where the rule
            applies. Good Friday and the n-th-weekday holidays never need it.
        """
        days = {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            easter_sunday(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 10, 0, 2),
            _observed(date(year, 11, 11)),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        }
        if year >= 2022:
            days.add(_observed(date(year, 6, 19)))
        return frozenset(days)

    def is_business_day(self, day: date) -> bool:
        """True when ``day`` is neither a weekend nor an observed holiday."""
        if day.weekday() >= 5:
            return False
        return day not in self.holidays(day.year)

    def next_business_day(self, day: date) -> date:
        """The first business day strictly after ``day``."""
        candidate = day + timedelta(days=1)
        while not self.is_business_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def previous_business_day(self, day: date) -> date:
        """The last business day strictly before ``day``."""
        candidate = day - timedelta(days=1)
        while not self.is_business_day(candidate):
            candidate -= timedelta(days=1)
        return candidate

    def adjust(self, day: date, convention: BusinessDayConvention) -> date:
        """Roll ``day`` to a business day under ``convention``.

        Args:
            day: Date to adjust; returned unchanged when already a business day.
            convention: Roll rule to apply.

        Returns:
            The adjusted business day.

        Raises:
            UnsupportedConventionError: ``convention`` is not a
                :class:`BusinessDayConvention`.
        """
        if not isinstance(convention, BusinessDayConvention):
            raise UnsupportedConventionError(
                f"business day convention {convention!r} is not implemented"
            )
        if self.is_business_day(day):
            return day
        if convention is BusinessDayConvention.PRECEDING:
            return self.previous_business_day(day)
        rolled = self.next_business_day(day)
        if convention is BusinessDayConvention.MODIFIED_FOLLOWING and rolled.month != day.month:
            return self.previous_business_day(day)
        return rolled

    def business_days(self, start: date, end: date) -> list[date]:
        """Business days in ``[start, end)``, in order.

        Args:
            start: First date considered, inclusive.
            end: Last date considered, exclusive.

        Returns:
            The business days, which is an empty list when ``end <= start``.
        """
        out: list[date] = []
        day = start
        while day < end:
            if self.is_business_day(day):
                out.append(day)
            day += timedelta(days=1)
        return out

    def add_business_days(self, day: date, count: int) -> date:
        """Advance ``count`` business days from ``day``; negative counts go back."""
        step = 1 if count >= 0 else -1
        remaining = abs(count)
        current = day
        while remaining:
            current += timedelta(days=step)
            if self.is_business_day(current):
                remaining -= 1
        return current


SIFMA_US = SIFMAUSCalendar()
"""The single :class:`SIFMAUSCalendar` instance the package uses."""


def accrual(start: date, end: date, day_count: DayCount) -> float:
    """Convenience alias of :func:`~rates_engine.conventions.daycount.year_fraction`, in years."""
    return year_fraction(start, end, day_count)
