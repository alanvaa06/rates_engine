"""Day counts, calendars, IMM dates and the refusal that replaces a default."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rates_engine.conventions.calendar import (
    SIFMA_US,
    BusinessDayConvention,
    HolidayCalendar,
    easter_sunday,
)
from rates_engine.conventions.daycount import DayCount, day_count_from_name, year_fraction
from rates_engine.conventions.schedule import Schedule, imm_date, imm_dates, next_imm_on_or_after
from rates_engine.core.errors import UnsupportedConventionError


class TestYearFraction:
    """PRD-001 AC-1.1: the same ninety days, three different answers."""

    START = date(2026, 1, 15)
    END = date(2026, 4, 15)

    def test_act_360_is_ninety_over_three_sixty(self):
        assert year_fraction(self.START, self.END, DayCount.ACT_360) == pytest.approx(
            90 / 360, abs=0.0
        )

    def test_act_365f_is_ninety_over_three_sixty_five(self):
        assert year_fraction(self.START, self.END, DayCount.ACT_365F) == pytest.approx(
            90 / 365, abs=0.0
        )

    def test_thirty_360_is_exactly_a_quarter(self):
        assert year_fraction(self.START, self.END, DayCount.THIRTY_360) == pytest.approx(
            0.25, abs=0.0
        )

    def test_thirty_360_handles_month_ends(self):
        # The 31st-start rule and the conditional 31st-end rule, separately.
        assert year_fraction(
            date(2026, 1, 31), date(2026, 2, 28), DayCount.THIRTY_360
        ) == pytest.approx(28 / 360)
        assert year_fraction(
            date(2026, 1, 30), date(2026, 3, 31), DayCount.THIRTY_360
        ) == pytest.approx(60 / 360)

    def test_reversed_dates_give_a_negative_fraction(self):
        assert year_fraction(self.END, self.START, DayCount.ACT_360) < 0


class TestUnsupportedConventions:
    """PRD-001 AC-1.4: name something unimplemented and get a refusal, never a default."""

    def test_unknown_name_refuses_and_names_it(self):
        with pytest.raises(UnsupportedConventionError, match="ACT/ACT ISMA"):
            day_count_from_name("ACT/ACT ISMA")

    def test_known_names_resolve_whatever_the_separator(self):
        assert day_count_from_name("act-360") is DayCount.ACT_360
        assert day_count_from_name(" ACT/365F ") is DayCount.ACT_365F
        assert day_count_from_name("30/360") is DayCount.THIRTY_360

    def test_a_bare_string_is_not_silently_accepted_as_a_day_count(self):
        with pytest.raises(UnsupportedConventionError, match="ACT/360"):
            year_fraction(date(2026, 1, 1), date(2026, 2, 1), "ACT/360")  # type: ignore[arg-type]

    def test_unknown_roll_convention_refuses(self):
        with pytest.raises(UnsupportedConventionError):
            SIFMA_US.adjust(date(2026, 7, 4), "roll_it_somewhere")  # type: ignore[arg-type]


class TestCalendar:
    """PRD-001 AC-1.2: the SIFMA holiday set, and the observance rule it turns on."""

    def test_fixture_matches_the_implemented_calendar(self, fixtures_dir):
        # Generated from the same rules, so this catches an accidental edit to
        # either side, not a wrong rule. The wrong-rule check is
        # tests/test_sifma_publication_record.py, against the SOFR publication
        # record; v0.3's fixture agreed with v0.3's wrong rules.
        import csv

        with (fixtures_dir / "sifma_holidays.csv").open(encoding="utf-8") as handle:
            recorded = {date.fromisoformat(row["date"]) for row in csv.DictReader(handle)}
        implemented: set[date] = set()
        for year in range(2022, 2031):
            implemented |= SIFMA_US.holidays(year)
        assert recorded == implemented

    def test_the_twelve_holidays_are_all_there(self):
        assert len(SIFMA_US.holidays(2026)) == 12

    def test_juneteenth_only_from_2022(self):
        assert date(2021, 6, 18) not in SIFMA_US.holidays(2021)
        assert len(SIFMA_US.holidays(2021)) == 11
        assert date(2022, 6, 20) in SIFMA_US.holidays(2022)

    def test_good_friday_is_two_days_before_easter(self):
        assert easter_sunday(2025) == date(2025, 4, 20)
        assert date(2025, 4, 18) in SIFMA_US.holidays(2025)

    # Every year from 2018 to 2040 in which the last Monday of May falls on the
    # 29th, 30th or 31st, with that Monday. Taken from QuantLib 1.44,
    # ``UnitedStates(UnitedStates.SOFR)``, which closes each of these dates and
    # keeps the Monday a week earlier open. Until v0.4 this package started its
    # search on 28 May and never stepped forward, so in exactly these ten years
    # it closed the Monday before and kept Memorial Day itself open. The
    # publication record agrees for the three that have happened: FRED has no
    # SOFR for 31 May 2021, 30 May 2022 or 29 May 2023, and has 0.01%, 0.78%
    # and 5.05% on the Mondays a week earlier.
    MEMORIAL_DAYS_AFTER_THE_28TH = (
        date(2021, 5, 31),
        date(2022, 5, 30),
        date(2023, 5, 29),
        date(2027, 5, 31),
        date(2028, 5, 29),
        date(2032, 5, 31),
        date(2033, 5, 30),
        date(2034, 5, 29),
        date(2038, 5, 31),
        date(2039, 5, 30),
    )

    @pytest.mark.parametrize("memorial_day", MEMORIAL_DAYS_AFTER_THE_28TH)
    def test_memorial_day_after_the_28th_is_the_holiday(self, memorial_day):
        assert memorial_day.weekday() == 0
        assert not SIFMA_US.is_business_day(memorial_day)
        assert SIFMA_US.is_business_day(memorial_day - timedelta(days=7))

    def test_memorial_day_is_a_monday_in_the_last_seven_days_of_may(self):
        # The closed form, independent of how the calendar searches for it: the
        # last Monday of a 31-day month is the one Monday among its last seven
        # days. Checked over more than a century so that every weekday 31 May
        # can fall on is covered many times over.
        for year in range(1990, 2101):
            in_may = [day for day in SIFMA_US.holidays(year) if day.month == 5]
            assert len(in_may) == 1, year
            assert in_may[0].weekday() == 0, year
            assert 25 <= in_may[0].day <= 31, year

    def test_saturday_holiday_observed_on_the_friday(self):
        # 4 July 2026 is a Saturday. PRD-001 AC-1.2 flagged this rule for manual
        # confirmation; the publication record has since confirmed it for
        # Independence Day and Christmas and refuted it for New Year's Day and
        # Veterans Day -- see TestSaturdayObservance.
        assert date(2026, 7, 4).weekday() == 5
        assert date(2026, 7, 3) in SIFMA_US.holidays(2026)
        assert not SIFMA_US.is_business_day(date(2026, 7, 3))

    def test_sunday_holiday_observed_on_the_monday(self):
        assert date(2027, 12, 25).weekday() == 5
        assert date(2022, 12, 25).weekday() == 6
        assert date(2022, 12, 26) in SIFMA_US.holidays(2022)

    def test_rolls(self):
        saturday = date(2026, 1, 17)
        assert SIFMA_US.adjust(saturday, BusinessDayConvention.FOLLOWING) == date(2026, 1, 20)
        assert SIFMA_US.adjust(saturday, BusinessDayConvention.PRECEDING) == date(2026, 1, 16)

    def test_modified_following_stays_inside_the_month(self):
        # 31 May 2026 is a Sunday; following would cross into June.
        end_of_may = date(2026, 5, 31)
        assert SIFMA_US.adjust(end_of_may, BusinessDayConvention.FOLLOWING).month == 6
        assert SIFMA_US.adjust(end_of_may, BusinessDayConvention.MODIFIED_FOLLOWING).month == 5

    def test_business_day_arithmetic_round_trips(self):
        start = date(2026, 3, 18)
        assert SIFMA_US.add_business_days(SIFMA_US.add_business_days(start, 5), -5) == start


class TestIMM:
    """PRD-001 AC-1.3: IMM dates are third Wednesdays of the quarterly months."""

    def test_imm_dates_are_third_wednesdays(self):
        for day in imm_dates(2026):
            assert day.weekday() == 2
            assert 15 <= day.day <= 21
        assert [d.month for d in imm_dates(2026)] == [3, 6, 9, 12]

    def test_known_imm_dates(self):
        assert imm_dates(2026) == (
            date(2026, 3, 18),
            date(2026, 6, 17),
            date(2026, 9, 16),
            date(2026, 12, 16),
        )

    def test_non_imm_month_refuses(self):
        with pytest.raises(ValueError, match="not an IMM month"):
            imm_date(2026, 4)

    def test_next_imm_includes_the_day_itself(self):
        assert next_imm_on_or_after(date(2026, 3, 18)) == date(2026, 3, 18)
        assert next_imm_on_or_after(date(2026, 3, 19)) == date(2026, 6, 17)
        assert next_imm_on_or_after(date(2026, 12, 17)) == date(2027, 3, 17)


class TestSchedule:
    """Schedules generate backwards, so a stub lands at the front."""

    def test_annual_schedule_spans_the_trade(self):
        schedule = Schedule.generate(
            date(2026, 3, 18), date(2028, 3, 15), frequency_months=12
        )
        assert len(schedule) == 2
        assert schedule.accrual_start[0] == date(2026, 3, 18)
        assert schedule.accrual_end[-1] == date(2028, 3, 15)

    def test_stub_is_at_the_front(self):
        schedule = Schedule.generate(
            date(2026, 1, 15), date(2028, 3, 15), frequency_months=12
        )
        first = (schedule.accrual_end[0] - schedule.accrual_start[0]).days
        last = (schedule.accrual_end[-1] - schedule.accrual_start[-1]).days
        assert first < last

    def test_payment_lag_pushes_payment_past_accrual_end(self):
        schedule = Schedule.generate(
            date(2026, 3, 18), date(2027, 3, 17), frequency_months=12, payment_lag_days=2
        )
        assert schedule.payment[-1] > schedule.accrual_end[-1]
        assert SIFMA_US.is_business_day(schedule.payment[-1])

    @pytest.mark.parametrize(
        "effective,maturity,frequency",
        [
            (date(2026, 3, 18), date(2026, 3, 18), 12),
            (date(2026, 3, 18), date(2026, 3, 17), 12),
        ],
    )
    def test_degenerate_schedules_refuse(self, effective, maturity, frequency):
        with pytest.raises(ValueError):
            Schedule.generate(effective, maturity, frequency_months=frequency)

    def test_non_positive_frequency_refuses(self):
        with pytest.raises(ValueError, match="frequency_months"):
            Schedule.generate(
                date(2026, 1, 1), date(2027, 1, 1) + timedelta(days=0), frequency_months=0
            )


class TestSaturdayObservance:
    """Which Saturday holidays close the Friday before, and which close nothing.

    Independence Day, Juneteenth and Christmas on a Saturday are observed on
    the Friday; New Year's Day and Veterans Day on a Saturday are not moved at
    all. A Sunday holiday moves to the Monday in all five cases.

    The publication record is the reference. FRED's SOFR series (downloaded
    2026-10-15) has a fixing for Friday 31 December 2021 (0.05%), the day
    before a Saturday New Year's Day, and for Friday 10 November 2023 (5.32%),
    the day before a Saturday Veterans Day; it has none for Friday 3 July
    2020 or Friday 24 December 2021. QuantLib 1.44 agrees on all four, in
    both ``UnitedStates.SOFR`` and ``UnitedStates.GovernmentBond``.

    v0.3 moved all five holidays off a Saturday, which closed 31 December 2021
    and 10 November 2023 and so threw away the fixings published on them. The
    test that pinned that rule -- "New Year on a Saturday is observed on the
    previous Friday", for 2022, 2028 and 2033 -- is the one replaced here.
    """

    @pytest.mark.parametrize(
        "friday", [date(2021, 12, 31), date(2027, 12, 31), date(2032, 12, 31)]
    )
    def test_new_year_on_a_saturday_leaves_the_friday_open(self, friday):
        assert date(friday.year + 1, 1, 1).weekday() == 5
        assert SIFMA_US.is_business_day(friday)

    @pytest.mark.parametrize(
        "friday", [date(2023, 11, 10), date(2028, 11, 10), date(2034, 11, 10)]
    )
    def test_veterans_day_on_a_saturday_leaves_the_friday_open(self, friday):
        assert date(friday.year, 11, 11).weekday() == 5
        assert SIFMA_US.is_business_day(friday)

    @pytest.mark.parametrize(
        "friday",
        [
            date(2020, 7, 3),    # Independence Day, Saturday 4 July 2020
            date(2026, 7, 3),    # Independence Day, Saturday 4 July 2026
            date(2021, 12, 24),  # Christmas, Saturday 25 December 2021
            date(2027, 12, 24),  # Christmas, Saturday 25 December 2027
            date(2027, 6, 18),   # Juneteenth, Saturday 19 June 2027
        ],
    )
    def test_the_other_three_on_a_saturday_close_the_friday(self, friday):
        assert (friday + timedelta(days=1)).weekday() == 5
        assert not SIFMA_US.is_business_day(friday)

    @pytest.mark.parametrize(
        "monday",
        [
            date(2023, 1, 2),    # New Year's Day, Sunday 1 January 2023
            date(2018, 11, 12),  # Veterans Day, Sunday 11 November 2018
            date(2021, 7, 5),    # Independence Day, Sunday 4 July 2021
            date(2022, 6, 20),   # Juneteenth, Sunday 19 June 2022
            date(2022, 12, 26),  # Christmas, Sunday 25 December 2022
        ],
    )
    def test_all_five_on_a_sunday_close_the_monday(self, monday):
        assert (monday - timedelta(days=1)).weekday() == 6
        assert not SIFMA_US.is_business_day(monday)

    def test_a_saturday_new_year_is_listed_without_closing_a_weekday(self):
        # The holiday is still SIFMA's, so it is in the list; it simply falls
        # on a day that was never a business day.
        assert date(2022, 1, 1) in SIFMA_US.holidays(2022)
        assert len(SIFMA_US.holidays(2022)) == 12
        days = SIFMA_US.business_days(date(2021, 12, 29), date(2022, 1, 5))
        assert days == [
            date(2021, 12, 29),
            date(2021, 12, 30),
            date(2021, 12, 31),
            date(2022, 1, 3),
            date(2022, 1, 4),
        ]


class TestObservanceAcrossAYearBoundary:
    """A v1 bug in the base class, kept pinned now that SIFMA cannot reach it.

    ``holidays(year)`` returns the dates a year's holidays are *observed on*,
    and a weekend rule can push one out of its own year. ``is_business_day``
    once looked only in ``holidays(day.year)``, so a holiday observed in the
    year before its own was reported as a business day. SIFMA's New Year's
    Day was the case that exposed it, under a Saturday rule that turned out to
    be wrong (see :class:`TestSaturdayObservance`); with that rule corrected
    no SIFMA holiday leaves its year. The guard in the base class stays,
    because a calendar with such a rule is still a calendar it has to serve,
    so it is tested here on one built for the purpose.
    """

    class _NewYearBackToFriday(HolidayCalendar):
        name = "new_year_back_to_friday"

        def holidays(self, year: int) -> frozenset[date]:
            new_year = date(year, 1, 1)
            if new_year.weekday() == 5:
                return frozenset({new_year - timedelta(days=1)})
            return frozenset({new_year})

    def test_a_holiday_observed_in_the_year_before_is_found(self):
        calendar = self._NewYearBackToFriday()
        assert date(2021, 12, 31) in calendar.holidays(2022)
        assert not calendar.is_business_day(date(2021, 12, 31))

    def test_the_roll_lands_past_the_boundary(self):
        calendar = self._NewYearBackToFriday()
        assert calendar.next_business_day(date(2021, 12, 30)) == date(2022, 1, 3)
        days = calendar.business_days(date(2021, 12, 29), date(2022, 1, 5))
        assert date(2021, 12, 31) not in days

    def test_no_sifma_holiday_leaves_its_own_year(self):
        for year in range(2000, 2061):
            assert all(day.year == year for day in SIFMA_US.holidays(year)), year

    def test_an_ordinary_year_end_is_still_a_business_day(self):
        # 31 December 2026 is a Thursday and no holiday is observed on it.
        assert SIFMA_US.is_business_day(date(2026, 12, 31))
