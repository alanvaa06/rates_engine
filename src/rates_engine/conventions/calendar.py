"""Business day calendars, with SIFMA's US recommendation implemented.

The calendar is data pretending to be code, which is why the holiday rules are
written out one function per holiday rather than compressed: a mis-entered rule
produces a curve that is wrong by a day's accrual on some nodes and right on
others, and that is far easier to review as twelve named rules than as a table.

**The manual check, done against the record.** ``SIFMA_US`` encodes the
twelve recommended US holidays and the observance rule used for the
government bond market: a Sunday holiday is observed on the following
Monday, and a Saturday one on the preceding Friday for Independence Day,
Juneteenth and Christmas only -- New Year's Day and Veterans Day on a
Saturday close nothing. PRD-001 AC-1.2 marked that rule ``[manual-check]``,
to be confirmed against SIFMA's published calendar, which the build
environment cannot reach. It has been confirmed instead against something
stricter: the New York Fed publishes SOFR for every U.S. government
securities business day, and the calendar agrees with that publication
record (FRED series ``SOFR``) on every weekday from
:attr:`SIFMAUSCalendar.VERIFIED_FROM` to
:attr:`SIFMAUSCalendar.VERIFIED_THROUGH`. The check found three wrong rules
in v0.3 -- Memorial Day, the Saturday rule for two of the five holidays, a
missing special closure -- and ``tests/test_sifma_publication_record.py``
now holds the calendar to the record.

**What the record cannot cover.** Dates after
:attr:`SIFMAUSCalendar.VERIFIED_THROUGH` come from the rules alone, and an
unscheduled closure -- a national day of mourning, a storm -- cannot be
known before it is announced. Juneteenth on a Saturday, first due on 19
June 2027, has not happened since SIFMA began observing it, so its Friday
rule is the one rule here the record has not exercised; QuantLib 1.44
applies the same one. Before April 2018 there is no SOFR record, and the
special closures of those years are not encoded.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.core.errors import UnsupportedConventionError

__all__ = [
    "BMV",
    "BMVCalendar",
    "BusinessDayConvention",
    "Calendar",
    "HolidayCalendar",
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
    """The last ``weekday`` of a month, with Monday as 0.

    Counted back from the month's last day, which is the only starting point
    that cannot miss the answer. Until v0.4 the search started on the 28th
    and stepped forward a week at a time, but 28 + 7 is always in the next
    month, so it never stepped: it returned the last ``weekday`` on or before
    the 28th. For Memorial Day that is a week early whenever the last Monday
    of May is the 29th, 30th or 31st -- ten years in every twenty-three, 2021
    to 2023 and 2027 among them.
    """
    following = date(year + month // 12, month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Saturday back to Friday, Sunday forward to Monday.

    SIFMA's rule for Independence Day, Juneteenth and Christmas. It is not
    its rule for New Year's Day or Veterans Day, which use
    :func:`_observed_monday_only`; v0.3 applied this one to all five.
    """
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _observed_monday_only(day: date) -> date:
    """Sunday forward to Monday; a Saturday holiday stays on the Saturday.

    SIFMA's rule for New Year's Day and Veterans Day. A Saturday one closes
    no weekday at all: the New York Fed published SOFR on Friday 31 December
    2021 (0.05%) and Friday 10 November 2023 (5.32%), the days before the
    two that have fallen on a Saturday since SOFR began (FRED series
    ``SOFR``), and QuantLib's US government bond and SOFR calendars keep
    both Fridays open. The Saturday is returned rather than dropped so that
    every year still lists its twelve holidays.
    """
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


class HolidayCalendar:
    """Everything a calendar does once it can answer "is this a holiday?".

    Subclasses supply :attr:`name` and :meth:`holidays`; rolling, counting
    and stepping are derived here. Extracted when the second calendar
    arrived, because the alternative was eighty lines of identical
    date-stepping in two places, where the two would eventually stop being
    identical without anyone noticing.
    """

    name: str = "unnamed"

    def holidays(self, year: int) -> frozenset[date]:
        """Every holiday observed in ``year``, as observed dates.

        "Observed dates" is the subtlety: a weekend-observance rule can move
        a holiday *out of its own year*. A rule that observed a Saturday New
        Year's Day on the Friday would put 31 December 2021 in
        ``holidays(2022)``, because it is the 2022 holiday. SIFMA's calendar
        has no such rule any more -- it leaves a Saturday New Year's Day where
        it is -- but the base class does not assume that of every calendar.
        :meth:`is_business_day` accounts for it; see there.
        """
        raise NotImplementedError

    def _observed_near(self, year: int) -> frozenset[date]:
        """Holidays that could fall in ``year``, cached.

        The union of three years, because an observance rule can push a
        holiday backwards or forwards across a year boundary. Cached per
        instance because calendars are stateless singletons and
        :meth:`business_days` would otherwise recompute the same sets on
        every step.
        """
        cache = self.__dict__.setdefault("_holiday_cache", {})
        if year not in cache:
            cache[year] = frozenset(self.holidays(year))
        nxt = self.__dict__["_holiday_cache"]
        for adjacent in (year - 1, year + 1):
            if adjacent not in nxt:
                nxt[adjacent] = frozenset(self.holidays(adjacent))
        return cache[year] | nxt[year - 1] | nxt[year + 1]

    def is_business_day(self, day: date) -> bool:
        """True when ``day`` is neither a weekend nor an observed holiday.

        Checks the neighbouring years as well as ``day``'s own, because an
        observance rule can move a holiday across a year boundary: under a
        rule that observes a Saturday New Year's Day on the Friday, the
        observed holiday is 31 December of the preceding year. Looking only
        in ``holidays(day.year)`` reported that Friday as a business day,
        which is the bug this guards. (SIFMA_US held that rule until v0.4,
        wrongly; no SIFMA holiday now leaves its own year, but the guard
        costs nothing and serves any calendar that does.)
        """
        if day.weekday() >= 5:
            return False
        return day not in self._observed_near(day.year)

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


class SIFMAUSCalendar(HolidayCalendar):
    """SIFMA's recommended US holiday schedule for the fixed income market.

    Twelve holidays. Juneteenth only from 2022, the year it became a federal
    holiday; asking about 2021 correctly reports 19 June as a business day.

    **It is the calendar SOFR fixes on**, and where that and SIFMA's
    recommendation part, it follows SOFR. The one place they do is Good
    Friday in a year when the employment report comes out that morning --
    2021, 2023 and 2026 so far. QuantLib's ``UnitedStates.GovernmentBond``
    keeps those days open, for SIFMA's early close, but the New York Fed
    published no SOFR on any of the three, so here they are holidays like
    every other Good Friday, as in QuantLib's ``UnitedStates.SOFR``. A
    fixing that was never published cannot be compounded, and a calendar
    that asks for one turns a valid period into ``MissingFixingError``.

    **Special closures** are listed in :attr:`SPECIAL_CLOSURES`, one date
    each with the reason, because no rule produces them. A day the market
    stayed open in spite of an occasion is not listed: 9 January 2025, the
    national day of mourning for President Carter, was one, and SOFR was
    published for it (4.30%).
    """

    name = "SIFMA_US"

    #: Closures no rule produces, each with the reason and the record that
    #: shows it. Only the stretch the publication record covers is listed;
    #: closures before SOFR began in April 2018 are not encoded.
    SPECIAL_CLOSURES: Mapping[date, str] = MappingProxyType(
        {
            date(2018, 12, 5): (
                "National day of mourning for President George H. W. Bush. No SOFR "
                "was published for it (FRED series SOFR); QuantLib 1.44 closes it in "
                "both UnitedStates.GovernmentBond and UnitedStates.SOFR."
            ),
        }
    )

    #: First weekday checked against the SOFR publication record: 3 April
    #: 2018, the first SOFR in FRED's series.
    VERIFIED_FROM = date(2018, 4, 3)

    #: Last weekday checked against the record, as downloaded from FRED on
    #: 15 October 2026. Moving it later needs the record extended to match;
    #: ``tests/test_sifma_publication_record.py`` fails until it is.
    VERIFIED_THROUGH = date(2026, 10, 14)

    def holidays(self, year: int) -> frozenset[date]:
        """Every holiday observed in ``year``, as observed dates.

        Args:
            year: Calendar year.

        Returns:
            The observed dates, already rolled off weekends where the rule
            applies, plus any :attr:`SPECIAL_CLOSURES` in the year. Good
            Friday and the n-th-weekday holidays never need a roll. New
            Year's Day and Veterans Day on a Saturday are returned on the
            Saturday, which closes no business day.
        """
        days = {
            _observed_monday_only(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            easter_sunday(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 10, 0, 2),
            _observed_monday_only(date(year, 11, 11)),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        }
        if year >= 2022:
            days.add(_observed(date(year, 6, 19)))
        days.update(day for day in self.SPECIAL_CLOSURES if day.year == year)
        return frozenset(days)


class BMVCalendar(HolidayCalendar):
    """The Mexican stock exchange's holiday schedule.

    **Named for what it is.** PRD-003 AC-1.2 asks for Banxico's calendar,
    which is the *banking* calendar and a different list. This is the BMV
    one, because that is what the research gate could establish: Banxico
    returns 403 from the build environment, and the reachable source —
    QuantLib's ``ql/time/calendars/mexico.cpp``, whose implementation class
    is literally ``BmvImpl`` and reports "Mexican stock exchange" — answers
    the neighbouring question. Building it and calling it Banxico would be a
    plausible list with the wrong name on it.

    The diff against Banxico's published list is `[manual-check]`, recorded
    in ``tests/fixtures/bmv_holidays.csv.provenance.json`` and outstanding.
    Where the two agree, nothing changes when it is done; where they differ,
    an MXN curve moves, which is exactly why the name matters now.

    **Thirteen rules.** Three of them moved to Monday observance in 2006
    under the *Ley Federal del Trabajo* reform: Constitution Day, Benito
    Juárez's birthday and Revolution Day are fixed dates through 2005 and
    the first, third and third Monday of their months thereafter. Asking
    about 2005 correctly reports 5 February, and about 2026 the first
    Monday.

    Inauguration Day is 1 October every sixth year from 2024 — the six-year
    presidential term, not an annual holiday.

    Unlike SIFMA's, these are not rolled off weekends: a fixed-date Mexican
    holiday falling on a Saturday is simply not observed. That is what the
    source implements, and inventing an observance rule would be the same
    error as renaming the calendar.
    """

    name = "BMV"

    #: The first inauguration under the current six-year cycle.
    INAUGURATION_BASE_YEAR = 2024
    #: The year the Monday-observance reform took effect.
    MONDAY_OBSERVANCE_FROM = 2006

    def holidays(self, year: int) -> frozenset[date]:
        """Every holiday observed in ``year``.

        Args:
            year: Calendar year.

        Returns:
            The dates. No weekend-observance roll is applied, because this
            calendar does not have one.
        """
        easter = easter_sunday(year)
        days = {
            date(year, 1, 1),
            easter - timedelta(days=3),  # Holy Thursday
            easter - timedelta(days=2),  # Good Friday
            date(year, 5, 1),
            date(year, 9, 16),
            date(year, 11, 2),
            date(year, 12, 12),
            date(year, 12, 25),
        }
        if year >= self.MONDAY_OBSERVANCE_FROM:
            days.add(_nth_weekday(year, 2, 0, 1))   # Constitution Day
            days.add(_nth_weekday(year, 3, 0, 3))   # Benito Juarez
            days.add(_nth_weekday(year, 11, 0, 3))  # Revolution Day
        else:
            days.add(date(year, 2, 5))
            days.add(date(year, 3, 21))
            days.add(date(year, 11, 20))
        if year >= self.INAUGURATION_BASE_YEAR and (year - self.INAUGURATION_BASE_YEAR) % 6 == 0:
            days.add(date(year, 10, 1))
        return frozenset(days)


SIFMA_US = SIFMAUSCalendar()
"""The single :class:`SIFMAUSCalendar` instance the package uses."""

BMV = BMVCalendar()
"""The single :class:`BMVCalendar` instance the package uses."""


def accrual(start: date, end: date, day_count: DayCount) -> float:
    """Convenience alias of :func:`~rates_engine.conventions.daycount.year_fraction`, in years."""
    return year_fraction(start, end, day_count)
