"""Where each currency settles: the payment-system calendars FX runs on.

A bond calendar says when securities trade; a settlement calendar says when
money moves. FX settles in the two currencies' payment systems, so the
dollar leg of a USD/MXN trade settles on the days Fedwire is open, which is
the Federal Reserve's holiday calendar and not SIFMA's. The two differ in
both directions, and each difference is a day of carry in a forward:

- SIFMA closes the bond market on Good Friday; the Federal Reserve is open.
- A federal holiday that falls on a Saturday closes nothing at the Federal
  Reserve, which opens as usual on the Friday before; SIFMA usually closes
  that Friday. Independence Day 2026 is the next case: Friday 3 July 2026 is
  a SIFMA holiday and a Fedwire business day.

Using SIFMA for the dollar leg put the USD/MXN spot date of a trade struck on
1 July 2026 on Monday 6 July instead of Friday 3 July: three days of carry,
about eighty pips on this pair, the same size as the error the spot date
exists to fix.

**The Federal Reserve's rules.** Eleven federal holidays: New Year's Day,
Martin Luther King Jr. Day (from 1983), Washington's Birthday, Memorial Day,
Juneteenth (from 2022), Independence Day, Labor Day, Columbus Day, Veterans
Day, Thanksgiving and Christmas. A fixed-date holiday on a Sunday is
observed on the Monday after; one on a Saturday is not observed at all.
There is no Good Friday. The rules are QuantLib's
``UnitedStates(UnitedStates.FederalReserve)``, and the tests hold this
calendar to that one's holiday list on every weekday from 1978 to 2100.
Earlier years are not encoded: Veterans Day sat on a Monday in October from
1971 to 1977, and before 1971 the Monday-holiday law did not exist yet.
Unscheduled closures — a national day of mourning — cannot be known in
advance and are not here.

This module is self-contained on purpose: it shares the
:class:`~rates_engine.conventions.calendar.HolidayCalendar` machinery and
nothing else, so a change to SIFMA's rules cannot move an FX spot date.
"""

from __future__ import annotations

from datetime import date, timedelta

from rates_engine.conventions.calendar import BMV, Calendar, HolidayCalendar
from rates_engine.core.money import Currency

__all__ = [
    "FederalReserveCalendar",
    "FEDERAL_RESERVE",
    "SETTLEMENT_CALENDARS",
]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` of a month, Monday being 0."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` of a month, Monday being 0, counted back from its last day.

    Counted back from the last day because that is the starting point that
    cannot miss: a search that starts on the 28th and steps a week forward
    never steps, and returns Memorial Day a week early whenever the last
    Monday of May is the 29th, 30th or 31st.
    """
    following = date(year + month // 12, month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _sunday_to_monday(day: date) -> date | None:
    """The Federal Reserve's observance: Sunday to Monday, Saturday to nothing."""
    if day.weekday() == 6:
        return day + timedelta(days=1)
    if day.weekday() == 5:
        return None
    return day


class FederalReserveCalendar(HolidayCalendar):
    """The Federal Reserve's holidays, on which Fedwire does not settle dollars.

    See the module docstring for the rules and where they differ from
    ``SIFMA_US``. The source the tests hold it to is QuantLib 1.44's
    ``UnitedStates(UnitedStates.FederalReserve)``.
    """

    name = "FEDERAL_RESERVE"

    def holidays(self, year: int) -> frozenset[date]:
        """The weekdays the Federal Reserve closes for ``year``'s holidays.

        Closures, as every calendar here lists them (the contract of
        :meth:`HolidayCalendar.holidays
        <rates_engine.conventions.calendar.HolidayCalendar.holidays>`): a
        holiday that closes no weekday is not in the set.

        Args:
            year: Calendar year.

        Returns:
            The observed dates. A Sunday holiday is the Monday after; a
            Saturday one is absent, because the Federal Reserve observes it
            on no weekday at all. Neither rule moves a date across a year.
        """
        fixed = [
            date(year, 1, 1),    # New Year's Day
            date(year, 7, 4),    # Independence Day
            date(year, 11, 11),  # Veterans Day
            date(year, 12, 25),  # Christmas
        ]
        if year >= 2022:
            fixed.append(date(year, 6, 19))  # Juneteenth
        days = {
            _nth_weekday(year, 2, 0, 3),   # Washington's Birthday
            _last_weekday(year, 5, 0),     # Memorial Day
            _nth_weekday(year, 9, 0, 1),   # Labor Day
            _nth_weekday(year, 10, 0, 2),  # Columbus Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        }
        if year >= 1983:
            days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
        for day in fixed:
            observed = _sunday_to_monday(day)
            if observed is not None:
                days.add(observed)
        return frozenset(days)


FEDERAL_RESERVE = FederalReserveCalendar()
"""The single :class:`FederalReserveCalendar` instance the package uses."""


SETTLEMENT_CALENDARS: dict[Currency, Calendar] = {
    Currency.USD: FEDERAL_RESERVE,
    Currency.MXN: BMV,
}
"""The calendar each currency settles on, used when a pair names none.

The Federal Reserve's for dollars, which is where Fedwire settles them.
``BMV`` for pesos, which is the stock exchange's calendar and the only
Mexican one this package has; Banxico's banking calendar, which SPEI
settles on, is a different list that has not been compared (see
:class:`~rates_engine.conventions.calendar.BMVCalendar`). QuantLib's
``Mexico()`` is the same BMV list. A currency missing from this table makes
a pair built without explicit calendars refuse rather than settle on a
calendar nobody chose.
"""
