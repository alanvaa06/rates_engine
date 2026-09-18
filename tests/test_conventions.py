"""Day counts, calendars, IMM dates and the refusal that replaces a default."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rates_engine.conventions import (
    SIFMA_US,
    BusinessDayConvention,
    DayCount,
    Schedule,
    day_count_from_name,
    easter_sunday,
    imm_date,
    imm_dates,
    next_imm_on_or_after,
    year_fraction,
)
from rates_engine.errors import UnsupportedConventionError


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
        # either side, not a wrong rule. The wrong-rule check is the
        # [manual-check] recorded in the fixture's provenance.
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

    def test_saturday_holiday_observed_on_the_friday(self):
        # 4 July 2026 is a Saturday. This is the rule PRD-001 AC-1.2 flags for manual
        # confirmation, so it is asserted explicitly rather than incidentally.
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
