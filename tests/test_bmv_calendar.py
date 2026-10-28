"""PRD-003 AC-1.2: the Mexican calendar, under the name it actually has.

The criterion asks for Banxico's calendar. This is the BMV's, because that
is what the research gate could establish — Banxico returns 403 from the
build environment, and the reachable source is QuantLib's
`ql/time/calendars/mexico.cpp`, whose implementation is named `BmvImpl` and
reports "Mexican stock exchange". The banking calendar and the exchange
calendar are different lists.

So these tests do two things. They pin the thirteen rules exactly, against
dates worked out by hand rather than by calling the code twice. And they
pin the *name*, because building this list and calling it Banxico is the
one failure mode that would make every MXN curve quietly wrong. The diff
against Banxico is `[manual-check]` and outstanding; the fixture's
provenance says so and names the two lines to confirm first.
"""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from rates_engine.conventions.calendar import (
    BMV,
    SIFMA_US,
    BMVCalendar,
    BusinessDayConvention,
    Calendar,
    HolidayCalendar,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bmv_holidays.csv"


class TestItSaysWhatItIs:
    """The name is load-bearing, not cosmetic."""

    def test_it_is_called_bmv_not_banxico(self):
        assert BMV.name == "BMV"
        assert "banxico" not in BMV.name.lower()

    def test_the_docstring_says_which_calendar_it_is_not(self):
        text = BMVCalendar.__doc__ or ""
        assert "banking" in text.lower()
        assert "manual-check" in text

    def test_the_fixture_provenance_records_the_check_as_outstanding(self):
        record = json.loads(FIXTURE.with_suffix(".csv.provenance.json").read_text())
        assert "NOT been done" in record["note"]
        assert "Banxico" in record["note"]

    def test_it_satisfies_the_calendar_protocol(self):
        assert isinstance(BMV, Calendar)


class TestTheFixedHolidays:
    """Eight dates that do not move."""

    @pytest.mark.parametrize(
        "day",
        [
            date(2026, 1, 1),    # New Year
            date(2026, 5, 1),    # Labour Day
            date(2026, 9, 16),   # National Day
            date(2026, 11, 2),   # All Souls
            date(2025, 12, 12),  # Our Lady of Guadalupe (a Saturday in 2026)
            date(2026, 12, 25),  # Christmas
        ],
    )
    def test_it_is_a_holiday(self, day):
        assert day.weekday() < 5
        assert day in BMV.holidays(day.year)
        assert not BMV.is_business_day(day)

    def test_easter_gives_holy_thursday_and_good_friday(self):
        """Easter 2026 is 5 April, so Holy Thursday is the 2nd and Good
        Friday the 3rd. Two days, not one — this is where the Mexican
        calendar differs from SIFMA's, which observes only Good Friday."""
        assert date(2026, 4, 2) in BMV.holidays(2026)
        assert date(2026, 4, 3) in BMV.holidays(2026)
        assert date(2026, 4, 2) not in SIFMA_US.holidays(2026)

    def test_a_year_lists_the_weekdays_its_rules_close(self):
        """Eleven dates in an ordinary year, twelve in an inauguration year,
        and ``holidays(year)`` keeps the ones that fall on a weekday, as
        SIFMA's does: a fixed-date holiday on a weekend is not observed, so
        it closes nothing. Worked by hand: 12 December 2026 is a Saturday;
        in 2024, 2 November is; in 2022, 1 January is a Saturday and 1 May
        and 25 December are Sundays."""
        assert len(BMV.holidays(2026)) == 10
        assert len(BMV.holidays(2024)) == 11
        assert len(BMV.holidays(2022)) == 8
        assert date(2026, 12, 12) not in BMV.holidays(2026)
        assert date(2024, 11, 2) not in BMV.holidays(2024)

    def test_every_listed_holiday_closes_a_weekday(self):
        """Review finding (round 3): until v0.4 BMV listed weekend dates such
        as Saturday 16 September 2023 and Saturday 12 December 2026, while
        SIFMA_US lists weekday closures only, so ``holidays(year)`` meant two
        different things on the two calendars. ``is_business_day`` did not
        change: a weekend was never a business day."""
        for year in range(1990, 2061):
            weekend = [day for day in BMV.holidays(year) if day.weekday() >= 5]
            assert weekend == [], year


class TestTheMondayObservanceReform:
    """Three holidays moved to a Monday rule in 2006. Both sides are pinned."""

    @pytest.mark.parametrize(
        "year,expected",
        # 5 February 2005 was a Saturday, which closes nothing and is not
        # listed; 2003 is the next year back with the 5th on a weekday.
        [(2003, date(2003, 2, 5)), (2004, date(2004, 2, 5))],
    )
    def test_constitution_day_is_fixed_through_2005(self, year, expected):
        assert expected.weekday() < 5
        assert expected in BMV.holidays(year)

    @pytest.mark.parametrize(
        "year,expected",
        [(2006, date(2006, 2, 6)), (2026, date(2026, 2, 2))],
    )
    def test_constitution_day_is_the_first_monday_from_2006(self, year, expected):
        assert expected in BMV.holidays(year)
        assert expected.weekday() == 0
        assert date(year, 2, 5) not in BMV.holidays(year) or date(year, 2, 5) == expected

    def test_benito_juarez_is_fixed_through_2005(self):
        assert date(2005, 3, 21) in BMV.holidays(2005)

    def test_benito_juarez_is_the_third_monday_from_2006(self):
        third = date(2026, 3, 16)
        assert third in BMV.holidays(2026)
        assert third.weekday() == 0

    def test_revolution_day_is_fixed_through_2005(self):
        # 20 November fell on a Sunday in 2005 and a Saturday in 2004, so
        # the last year the fixed date closed a weekday was 2003 (Thursday).
        assert date(2003, 11, 20).weekday() == 3
        assert date(2003, 11, 20) in BMV.holidays(2003)

    def test_revolution_day_is_the_third_monday_from_2006(self):
        third = date(2026, 11, 16)
        assert third in BMV.holidays(2026)
        assert third.weekday() == 0

    def test_the_boundary_year_is_2006_not_2005(self):
        """Pinned on weekdays the two rules disagree about. In 2005 the
        Monday rule would have closed Monday 7 February and Monday 21
        November; the fixed dates, Saturday 5 February and Sunday 20
        November, closed nothing, so both Mondays were ordinary days. In
        2006 the Monday rule closes Monday 20 March, and the fixed date,
        Tuesday 21 March, is open."""
        assert BMV.is_business_day(date(2005, 2, 7))
        assert BMV.is_business_day(date(2005, 11, 21))
        assert date(2006, 2, 6) in BMV.holidays(2006)
        assert not BMV.is_business_day(date(2006, 3, 20))
        assert BMV.is_business_day(date(2006, 3, 21))


class TestInaugurationDay:
    """Once every six years, not annually."""

    @pytest.mark.parametrize("year", [2024, 2030, 2036])
    def test_it_falls_in_an_inauguration_year(self, year):
        assert date(year, 10, 1) in BMV.holidays(year)

    @pytest.mark.parametrize("year", [2023, 2025, 2026, 2029, 2031])
    def test_it_does_not_fall_in_any_other_year(self, year):
        assert date(year, 10, 1) not in BMV.holidays(year)

    def test_it_does_not_exist_before_2024(self):
        assert date(2018, 10, 1) not in BMV.holidays(2018)


class TestNoWeekendObservance:
    """The rule this calendar does *not* have, asserted so nobody adds one."""

    def test_a_saturday_holiday_is_not_moved_to_friday(self):
        """1 January 2022 was a Saturday. The BMV rules carry no roll, so 31
        December 2021 stays a business day. Inventing an observance rule here
        would be the same error as renaming the calendar.

        The Saturday itself is not listed either: it closes nothing, and
        ``holidays(year)`` is the weekdays a calendar closes."""
        assert date(2022, 1, 1).weekday() == 5
        assert date(2022, 1, 1) not in BMV.holidays(2022)
        assert date(2021, 12, 31) not in BMV.holidays(2021)
        assert date(2021, 12, 31) not in BMV.holidays(2022)
        assert BMV.is_business_day(date(2021, 12, 31))
        assert not BMV.is_business_day(date(2022, 1, 1))
        assert BMV.is_business_day(date(2022, 1, 3))

    def test_sifma_does_roll_and_the_two_therefore_differ(self):
        """Christmas 2021 was a Saturday. SIFMA observes it on Friday 24
        December -- FRED has no SOFR that day -- while the BMV calendar has
        no roll, so the same Friday is an ordinary business day there.

        This used to be New Year's Day 2022, on the belief that SIFMA closes
        the Friday before a Saturday 1 January. It does not: SOFR was
        published on 31 December 2021, and SIFMA_US no longer closes it."""
        assert date(2021, 12, 25).weekday() == 5
        assert date(2021, 12, 24) in SIFMA_US.holidays(2021)
        assert not SIFMA_US.is_business_day(date(2021, 12, 24))
        assert BMV.is_business_day(date(2021, 12, 24))


class TestDerivedBehaviour:
    """What HolidayCalendar gives every calendar, exercised on this one."""

    def test_weekends_are_not_business_days(self):
        assert not BMV.is_business_day(date(2026, 9, 19))  # Saturday
        assert not BMV.is_business_day(date(2026, 9, 20))  # Sunday

    def test_next_business_day_steps_over_a_holiday(self):
        # 16 September 2026 is a Wednesday and National Day.
        assert BMV.next_business_day(date(2026, 9, 15)) == date(2026, 9, 17)

    def test_previous_business_day_steps_back_over_one(self):
        assert BMV.previous_business_day(date(2026, 9, 17)) == date(2026, 9, 15)

    def test_following_rolls_forward(self):
        assert BMV.adjust(date(2026, 9, 16), BusinessDayConvention.FOLLOWING) == date(
            2026, 9, 17
        )

    def test_preceding_rolls_back(self):
        assert BMV.adjust(date(2026, 9, 16), BusinessDayConvention.PRECEDING) == date(
            2026, 9, 15
        )

    def test_modified_following_stays_in_the_month(self):
        """Christmas 2026 is a Friday; the following business day is 28
        December, still in December, so modified following does not turn."""
        assert BMV.adjust(
            date(2026, 12, 25), BusinessDayConvention.MODIFIED_FOLLOWING
        ) == date(2026, 12, 28)

    def test_a_business_day_is_returned_unchanged(self):
        assert BMV.adjust(date(2026, 9, 15), BusinessDayConvention.FOLLOWING) == date(
            2026, 9, 15
        )

    def test_business_days_excludes_the_end(self):
        days = BMV.business_days(date(2026, 9, 14), date(2026, 9, 19))
        assert days == [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 17), date(2026, 9, 18)]

    def test_add_business_days_skips_holidays(self):
        assert BMV.add_business_days(date(2026, 9, 15), 1) == date(2026, 9, 17)
        assert BMV.add_business_days(date(2026, 9, 17), -1) == date(2026, 9, 15)


class TestTheSharedBaseDidNotChangeSIFMA:
    """Extracting HolidayCalendar was a refactor, so SIFMA must be untouched."""

    def test_sifma_still_has_its_own_rules(self):
        assert SIFMA_US.name == "SIFMA_US"
        assert date(2026, 7, 4) in {d for d in SIFMA_US.holidays(2026)} or date(
            2026, 7, 3
        ) in SIFMA_US.holidays(2026)

    def test_both_calendars_share_one_implementation_of_the_derived_methods(self):
        assert isinstance(SIFMA_US, HolidayCalendar)
        assert isinstance(BMV, HolidayCalendar)
        assert type(SIFMA_US).next_business_day is type(BMV).next_business_day

    def test_the_base_refuses_to_answer_without_holidays(self):
        with pytest.raises(NotImplementedError):
            HolidayCalendar().holidays(2026)


class TestTheFixtureMatchesTheCode:
    """The dumped list is for human review, so it has to be the real one."""

    @pytest.fixture
    def rows(self):
        with FIXTURE.open(encoding="utf-8") as handle:
            return [
                (int(r["year"]), date.fromisoformat(r["date"]), r["weekday"])
                for r in csv.DictReader(handle)
            ]

    def test_every_row_is_a_holiday_the_code_reports(self, rows):
        for year, day, _ in rows:
            assert day in BMV.holidays(year)

    def test_no_holiday_is_missing_from_the_fixture(self, rows):
        dumped = {(y, d) for y, d, _ in rows}
        for year in range(2022, 2032):
            for day in BMV.holidays(year):
                assert (year, day) in dumped

    def test_the_weekday_column_is_right(self, rows):
        """Compared against a spelled-out list rather than `strftime("%A")`,
        which is locale-dependent: on a runner with a non-English locale the
        committed fixture and the computed name would differ for a reason
        nobody could act on. The CI matrix includes Windows."""
        names = (
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
        )
        for _, day, weekday in rows:
            assert names[day.weekday()] == weekday

    def test_it_spans_a_decade_including_two_inaugurations(self, rows):
        years = {y for y, _, _ in rows}
        assert years == set(range(2022, 2032))
        inaugurations = {d for _, d, _ in rows if d.month == 10 and d.day == 1}
        assert inaugurations == {date(2024, 10, 1), date(2030, 10, 1)}


class TestNoAccidentalSharing:
    """The two calendars must not be the same list by accident."""

    def test_they_disagree_on_plenty_of_days(self):
        differing = [
            day
            for day in (date(2026, 1, 1) + timedelta(days=k) for k in range(365))
            if SIFMA_US.is_business_day(day) != BMV.is_business_day(day)
        ]
        assert len(differing) >= 8

    def test_mexican_national_day_is_a_us_business_day(self):
        assert not BMV.is_business_day(date(2026, 9, 16))
        assert SIFMA_US.is_business_day(date(2026, 9, 16))

    def test_us_independence_day_is_a_mexican_business_day(self):
        assert BMV.is_business_day(date(2026, 7, 3))
