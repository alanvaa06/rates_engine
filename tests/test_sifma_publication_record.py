"""SIFMA_US against the SOFR publication record, one weekday at a time.

The other calendar tests check rules against rules. This one checks the
calendar against a record nobody here wrote. The New York Fed publishes SOFR
for every U.S. government securities business day, so a weekday with no SOFR
is a day the market SOFR measures was shut, and a weekday with one is a day
it was open. Over the stretch the record covers, ``SIFMA_US`` has to say
exactly the same thing, both ways round: a holiday the calendar misses drops
a published fixing, and a holiday it invents asks for one that never existed.

**Where the list comes from.** FRED, Federal Reserve Bank of St. Louis,
series ``SOFR``, downloaded on 2026-10-15 from
``https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR&cosd=2018-04-02&coed=2026-10-14``.
The file has one row per weekday from 3 April 2018, the first SOFR FRED
carries, to 14 October 2026 -- 2,227 rows, none on a weekend, SHA-256
``72fab9b37b95eae2392574cbeae5022c131c58c704f59cda5b3527921006144f`` -- and
leaves the value empty on a weekday when nothing was published.
:data:`NO_SOFR_PUBLISHED` is every such row, copied from the file and not
generated from this package; the other 2,131 weekdays all carry a rate.

What the record settled, against the rules this package had in v0.3:

* Memorial Day is the last Monday of May: no SOFR on 31 May 2021, 30 May
  2022 or 29 May 2023, and a fixing on the Monday a week before each.
* New Year's Day and Veterans Day on a Saturday close nothing: SOFR was
  published on Friday 31 December 2021 (0.05%) and Friday 10 November 2023
  (5.32%).
* 5 December 2018, the National Day of Mourning for President George H. W.
  Bush, was a full closure that no rule produces.
* Good Friday is closed even in 2021, 2023 and 2026, when the employment
  report came out that morning and QuantLib's ``GovernmentBond`` calendar
  keeps the bond market open: no SOFR was published on any of the three.
* 9 January 2025, the National Day of Mourning for President Carter, was
  not a closure: SOFR was published (4.30%).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rates_engine.conventions.calendar import SIFMA_US

RECORD_FIRST = date(2018, 4, 3)
"""First row of the FRED file: the first SOFR the series carries."""

RECORD_LAST = date(2026, 10, 14)
"""Last row of the FRED file as downloaded."""

NO_SOFR_PUBLISHED: tuple[date, ...] = tuple(
    date.fromisoformat(day)
    for day in (
        # 2018 (from 3 April): 8
        "2018-05-28", "2018-07-04", "2018-09-03", "2018-10-08",
        "2018-11-12", "2018-11-22", "2018-12-05", "2018-12-25",
        # 2019: 11
        "2019-01-01", "2019-01-21", "2019-02-18", "2019-04-19",
        "2019-05-27", "2019-07-04", "2019-09-02", "2019-10-14",
        "2019-11-11", "2019-11-28", "2019-12-25",
        # 2020: 11
        "2020-01-01", "2020-01-20", "2020-02-17", "2020-04-10",
        "2020-05-25", "2020-07-03", "2020-09-07", "2020-10-12",
        "2020-11-11", "2020-11-26", "2020-12-25",
        # 2021: 11
        "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02",
        "2021-05-31", "2021-07-05", "2021-09-06", "2021-10-11",
        "2021-11-11", "2021-11-25", "2021-12-24",
        # 2022: 11 (1 January was a Saturday and closed nothing)
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30",
        "2022-06-20", "2022-07-04", "2022-09-05", "2022-10-10",
        "2022-11-11", "2022-11-24", "2022-12-26",
        # 2023: 11 (11 November was a Saturday and closed nothing)
        "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07",
        "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04",
        "2023-10-09", "2023-11-23", "2023-12-25",
        # 2024: 12
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29",
        "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02",
        "2024-10-14", "2024-11-11", "2024-11-28", "2024-12-25",
        # 2025: 12
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
        "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
        "2025-10-13", "2025-11-11", "2025-11-27", "2025-12-25",
        # 2026 (to 14 October): 9
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-10-12",
    )
)
"""Every weekday from :data:`RECORD_FIRST` to :data:`RECORD_LAST` on which FRED has no SOFR."""


def _weekdays(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


class TestTheTranscription:
    """The constant is what the file said, before anything is checked against it."""

    def test_ninety_six_distinct_weekdays_in_order(self):
        assert len(NO_SOFR_PUBLISHED) == 96
        assert list(NO_SOFR_PUBLISHED) == sorted(set(NO_SOFR_PUBLISHED))
        assert all(day.weekday() < 5 for day in NO_SOFR_PUBLISHED)

    def test_all_inside_the_record(self):
        assert all(RECORD_FIRST <= day <= RECORD_LAST for day in NO_SOFR_PUBLISHED)

    def test_the_file_had_one_row_per_weekday(self):
        # 2,227 rows, of which 96 empty: the 2,131 published fixings.
        assert len(_weekdays(RECORD_FIRST, RECORD_LAST)) == 2227


class TestTheCalendarAgreesWithTheRecord:
    """PRD-001 AC-1.2's manual check, done against the record instead of by hand."""

    def test_every_weekday_agrees(self):
        closed = set(NO_SOFR_PUBLISHED)
        disagreements = [
            (day.isoformat(), "no SOFR published" if day in closed else "SOFR published")
            for day in _weekdays(RECORD_FIRST, RECORD_LAST)
            if SIFMA_US.is_business_day(day) == (day in closed)
        ]
        assert disagreements == []

    def test_the_calendar_claims_exactly_the_stretch_the_record_covers(self):
        # Widening the verified stretch without widening the record would let
        # the calendar claim a check nobody ran, so the two are tied here.
        assert SIFMA_US.VERIFIED_FROM == RECORD_FIRST
        assert SIFMA_US.VERIFIED_THROUGH == RECORD_LAST

    def test_the_bush_day_of_mourning_is_a_special_closure(self):
        assert not SIFMA_US.is_business_day(date(2018, 12, 5))
        assert date(2018, 12, 5) in SIFMA_US.SPECIAL_CLOSURES
        assert date(2018, 12, 5) in SIFMA_US.holidays(2018)

    def test_the_carter_day_of_mourning_was_not_a_closure(self):
        assert SIFMA_US.is_business_day(date(2025, 1, 9))

    @pytest.mark.parametrize("good_friday", [date(2021, 4, 2), date(2023, 4, 7), date(2026, 4, 3)])
    def test_good_friday_with_an_employment_report_is_still_closed(self, good_friday):
        assert not SIFMA_US.is_business_day(good_friday)
