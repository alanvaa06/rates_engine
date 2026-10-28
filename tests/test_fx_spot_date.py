"""The spot date: when a USD/MXN spot trade settles, and on whose calendars.

A quoted spot is the price for delivery on the spot date, not on the trade
date, and a forward's points run from there. No expected date here comes
from this package:

- the holiday lists are QuantLib 1.44's
  ``ql.UnitedStates(ql.UnitedStates.FederalReserve)`` for the dollar and
  ``ql.Mexico()`` for the peso;
- the spot rule is ObjectLab Kit's for the Mexican peso, applied by hand to
  those lists: T+1 has to be a US business day as well as a peso one, so
  every day of the lag is open in both, counted from the trade date
  (https://objectlabkit.sourceforge.net/currency.html; Wikipedia's "Foreign
  exchange date conventions" gives the same Latin American exception). The
  hand computation agrees with ``ql.JointCalendar(fed, mex).advance(trade,
  2, Days)`` on all 730 days of 2026-2027, and with the ``earliestDate()``
  of an ``FxSwapRateHelper`` given that joint calendar as its fixing
  calendar on all 488 trade dates both are open.

QuantLib's ``FxSwapRateHelper`` with ``Mexico()`` as its calendar and the
Federal Reserve as its ``tradingCalendar`` is the general dollar rule, under
which a US holiday on T+1 alone does not delay the spot. It is right for
most dollar pairs and wrong for this one, so it is the reference only for
the general rule (:data:`GENERIC_DOLLAR_RULE_OFFSETS`); round 2 of PR #13
pinned it for USD/MXN, and 16 peso business days of 2026-2027 settled a day
or more early (Alan Vazquez's correction to his own round-1 finding 1).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pytest

from rates_engine.conventions import currency_pair
from rates_engine.conventions.calendar import BMV, SIFMA_US
from rates_engine.conventions.currency_pair import (
    JOINT_LAG_CURRENCIES,
    USDMXN,
    CurrencyPair,
)
from rates_engine.conventions.settlement import (
    FEDERAL_RESERVE,
    SETTLEMENT_CALENDARS,
)
from rates_engine.core.money import Currency

FIRST, LAST = date(2026, 1, 1), date(2027, 12, 31)

# (spot date - trade date) in days for every trade date from 2026-01-01 to
# 2027-12-31: ObjectLab's peso rule applied by hand to QuantLib 1.44's two
# holiday lists, as the module docstring describes.
OBJECTLAB_SPOT_OFFSETS = (
    "4432222443222255432224432222554322244322225543222443222244322"
    "2244322225543222443222244322665432222443222244322224432225543"
    "2222443222244322225543222443222244322224432225543222244322224"
    "4322224432222443222244322224432222443222244322224432222443222"
    "2554322244323324432222443222244322225543222443222244322225543"
    "2224432332554322244322354432222443222244322224432225543222554"
    "3222244322225543222443222255432224432222554322244322224432222"
    "4432222554322244322665432222443222244322224432222443222244322"
    "2244322224432222443222255432224432222443222244322224432222554"
    "3222443222244322224432222443222244322224432222443222244322225"
    "5432224432235443222244322224432222554322244322224432222454332"
    "24432236554322244322354432222443222244322224432222443222244"
)

# The same days under the general dollar rule: QuantLib 1.44's
# FxSwapRateHelper with Mexico() as its calendar and FederalReserve as its
# tradingCalendar, earliestDate() - trade date. Two peso business days, then
# rolled to a day both are open. Round 2 pinned this for USD/MXN.
GENERIC_DOLLAR_RULE_OFFSETS = (
    "4432222443222254322224432222554322244322225432222443222244322"
    "2244322225543222443222244322665432222443222244322224432225543"
    "2222443222244322225432222443222244322224432225443222244322224"
    "4322224432222443222244322224432222443222244322224432222443222"
    "2543222244323324432222443222244322225432222443222244322225543"
    "2224432322554322244322324432222443222244322224432225543222554"
    "3222244322225432222443222255432224432222543222244322224432222"
    "4432222554322244322665432222443222244322224432222443222244322"
    "2244322224432222443222254322224432222443222244322224432222543"
    "2222443222244322224432222443222244322224432222443222244322225"
    "4322224432235443222244322224432222543222244322224432222454332"
    "24432232554322244322324432222443222244322224432222443222244"
)

#: The 16 peso business days of 2026-2027 the peso exception moves:
#: (trade date, general-rule spot, ObjectLab spot). In every one the general
#: rule's T+1 is a US holiday. Computed by hand as the module docstring says;
#: 2026-01-16, 2026-11-25 and 2027-11-10 are the three Alan Vazquez named.
MOVED_BY_THE_PESO_EXCEPTION = [
    (date(2026, 1, 16), date(2026, 1, 20), date(2026, 1, 21)),  # Martin Luther King Day
    (date(2026, 2, 13), date(2026, 2, 17), date(2026, 2, 18)),  # Washington's Birthday
    (date(2026, 5, 22), date(2026, 5, 26), date(2026, 5, 27)),  # Memorial Day
    (date(2026, 6, 18), date(2026, 6, 22), date(2026, 6, 23)),  # Juneteenth
    (date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)),  # Labor Day
    (date(2026, 10, 9), date(2026, 10, 13), date(2026, 10, 14)),  # Columbus Day
    (date(2026, 11, 10), date(2026, 11, 12), date(2026, 11, 13)),  # Veterans Day
    (date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)),  # Thanksgiving
    (date(2027, 1, 15), date(2027, 1, 19), date(2027, 1, 20)),  # Martin Luther King Day
    (date(2027, 2, 12), date(2027, 2, 16), date(2027, 2, 17)),  # Washington's Birthday
    (date(2027, 5, 28), date(2027, 6, 1), date(2027, 6, 2)),  # Memorial Day
    (date(2027, 7, 2), date(2027, 7, 6), date(2027, 7, 7)),  # 4 July, observed on the 5th
    (date(2027, 9, 3), date(2027, 9, 7), date(2027, 9, 8)),  # Labor Day
    (date(2027, 10, 8), date(2027, 10, 12), date(2027, 10, 13)),  # Columbus Day
    (date(2027, 11, 10), date(2027, 11, 12), date(2027, 11, 16)),  # Veterans + Revolution Day
    (date(2027, 11, 24), date(2027, 11, 26), date(2027, 11, 29)),  # Thanksgiving
]

SPOT_DATES = [
    # (trade date, ObjectLab spot date, why it is interesting)
    (date(2026, 1, 15), date(2026, 1, 20), "Martin Luther King Day on T+2: rolled to the 20th"),
    (date(2026, 1, 16), date(2026, 1, 21), "Martin Luther King Day on T+1: not a day of the lag"),
    (date(2026, 2, 13), date(2026, 2, 18), "Washington's Birthday on T+1"),
    (date(2026, 3, 31), date(2026, 4, 6), "Holy Thursday and Good Friday in Mexico"),
    (date(2026, 5, 22), date(2026, 5, 27), "Memorial Day on T+1"),
    (date(2026, 6, 17), date(2026, 6, 22), "Juneteenth on T+2"),
    (date(2026, 6, 18), date(2026, 6, 23), "Juneteenth on T+1"),
    (date(2026, 7, 1), date(2026, 7, 3), "4 July on a Saturday: the Federal Reserve opens on the 3rd"),
    (date(2026, 7, 2), date(2026, 7, 6), "a Friday before a Saturday holiday"),
    (date(2026, 9, 14), date(2026, 9, 17), "16 September in Mexico: T+3"),
    (date(2026, 10, 8), date(2026, 10, 13), "Columbus Day on T+2"),
    (date(2026, 11, 10), date(2026, 11, 13), "Veterans Day on T+1"),
    (date(2026, 11, 12), date(2026, 11, 17), "Revolution Day, third Monday of November"),
    (date(2026, 11, 25), date(2026, 11, 30), "Thanksgiving on T+1: Alan Vazquez's corrected case"),
    (date(2026, 12, 23), date(2026, 12, 28), "Christmas on T+2"),
    (date(2027, 6, 17), date(2027, 6, 21), "Juneteenth 2027 on a Saturday: not observed"),
    (date(2027, 11, 10), date(2027, 11, 16), "Veterans Day on T+1, Revolution Day on T+2"),
    (date(2027, 12, 30), date(2028, 1, 3), "across the year end"),
]


def _offsets(pair: CurrencyPair) -> str:
    return "".join(
        str((pair.spot_date(FIRST + timedelta(days=k)) - (FIRST + timedelta(days=k))).days)
        for k in range((LAST - FIRST).days + 1)
    )


def _mismatches(got: str, expected: str) -> list[str]:
    return [
        (FIRST + timedelta(days=k)).isoformat()
        for k, (a, b) in enumerate(zip(got, expected, strict=True))
        if a != b
    ]


class TestTheSpotDate:
    def test_every_trade_date_of_2026_and_2027_follows_the_peso_rule(self):
        assert _mismatches(_offsets(USDMXN), OBJECTLAB_SPOT_OFFSETS) == []

    @pytest.mark.parametrize(("trade", "expected", "why"), SPOT_DATES, ids=[c[2] for c in SPOT_DATES])
    def test_the_named_cases(self, trade, expected, why):
        assert USDMXN.spot_date(trade) == expected, why

    def test_a_us_holiday_on_t_plus_one_delays_the_peso(self):
        """Alan Vazquez's corrected case. Round 2 settled it on Friday the
        27th, three days early: about 82 pips on a three-month forward."""
        assert not FEDERAL_RESERVE.is_business_day(date(2026, 11, 26))
        assert BMV.is_business_day(date(2026, 11, 26))
        assert USDMXN.spot_date(date(2026, 11, 25)) == date(2026, 11, 30)

    def test_objectlabs_worked_example(self):
        """ObjectLab's example is EUR/MXN struck on Monday 3 July 2006, which
        settles on Thursday the 6th because 4 July cannot be T+1 for the
        peso. This build has no euro (`Currency` holds USD and MXN, and a
        pair needs two of them), so no cross can be built; the dollar leg of
        that example is the same calculation, and TARGET is open on all four
        days, so the euro moves nothing."""
        assert not FEDERAL_RESERVE.is_business_day(date(2006, 7, 4))
        assert BMV.is_business_day(date(2006, 7, 4))
        assert USDMXN.spot_date(date(2006, 7, 3)) == date(2006, 7, 6)

    def test_a_us_holiday_on_the_spot_date_still_rolls_it(self):
        assert USDMXN.spot_date(date(2026, 1, 15)) == date(2026, 1, 20)

    def test_a_mexican_holiday_inside_the_lag_does_not_count(self):
        assert USDMXN.spot_date(date(2026, 9, 14)) == date(2026, 9, 17)

    def test_the_pair_states_its_lag_and_calendars(self):
        assert USDMXN.spot_lag_days == 2
        assert USDMXN.calendars == (FEDERAL_RESERVE, BMV)
        assert USDMXN.lag_calendars == (FEDERAL_RESERVE, BMV)

    def test_a_zero_lag_rolls_the_trade_date_to_a_joint_business_day(self):
        same_day = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=0)
        assert same_day.spot_date(date(2026, 10, 8)) == date(2026, 10, 8)
        assert same_day.spot_date(date(2026, 9, 16)) == date(2026, 9, 17)
        assert same_day.spot_date(date(2026, 11, 26)) == date(2026, 11, 27)

    def test_one_day(self):
        next_day = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=1)
        assert next_day.spot_date(date(2026, 9, 15)) == date(2026, 9, 17)
        assert next_day.spot_date(date(2026, 11, 25)) == date(2026, 11, 27)


class TestThePesoExceptionToTheDollarRule:
    """The general rule for a dollar pair, and the list it does not apply to."""

    def test_the_list_is_the_documented_one(self):
        assert frozenset({"ARS", "CLP", "MXN"}) == JOINT_LAG_CURRENCIES

    def test_exactly_sixteen_peso_business_days_move(self):
        """Alan Vazquez counted 16; the hand computation finds the same 16,
        and they are the only business days where the two rules differ."""
        assert len(MOVED_BY_THE_PESO_EXCEPTION) == 16
        moved = {
            (FIRST + timedelta(days=k))
            for k, (a, b) in enumerate(
                zip(OBJECTLAB_SPOT_OFFSETS, GENERIC_DOLLAR_RULE_OFFSETS, strict=True)
            )
            if a != b and BMV.is_business_day(FIRST + timedelta(days=k))
        }
        assert moved == {trade for trade, _, _ in MOVED_BY_THE_PESO_EXCEPTION}

    @pytest.mark.parametrize(
        ("trade", "generic", "objectlab"),
        MOVED_BY_THE_PESO_EXCEPTION,
        ids=[t.isoformat() for t, _, _ in MOVED_BY_THE_PESO_EXCEPTION],
    )
    def test_each_moved_date_had_a_us_holiday_on_t_plus_one(self, trade, generic, objectlab):
        t_plus_one = trade + timedelta(days=1)
        while not BMV.is_business_day(t_plus_one):
            t_plus_one += timedelta(days=1)
        assert not FEDERAL_RESERVE.is_business_day(t_plus_one)
        assert USDMXN.spot_date(trade) == objectlab
        assert objectlab > generic

    def test_a_dollar_pair_off_the_list_counts_on_the_other_calendar_alone(self, monkeypatch):
        """The general rule is kept for dollar pairs whose other currency is
        not on the list. This build has no such currency yet, so the peso is
        taken off the list to exercise it, and it reproduces QuantLib's
        FxSwapRateHelper on every day of 2026-2027."""
        monkeypatch.setattr(currency_pair, "JOINT_LAG_CURRENCIES", frozenset())
        assert USDMXN.lag_calendars == (BMV,)
        assert _mismatches(_offsets(USDMXN), GENERIC_DOLLAR_RULE_OFFSETS) == []
        assert USDMXN.spot_date(date(2026, 11, 25)) == date(2026, 11, 27)


class TestWhereThisDiffersFromQuantLibOnPurpose:
    """On a trade date one of the calendars is shut, QuantLib's FxSwapRateHelper
    — given the joint calendar, which is how it reproduces the peso rule —
    first moves the trade date to the next joint business day and counts the
    lag from there; this package counts from the trade date itself. A spot
    quoted on 16 September (USD/MXN trades offshore on Mexican holidays)
    settles two joint business days later, on the 18th; the helper says the
    21st. On a Saturday the last quote is Friday's, which settles on the
    Tuesday; the helper counts from Monday. The 242 such days of 2026-2027 are
    the only ones where the two differ, and these are its answers."""

    @pytest.mark.parametrize(
        ("trade", "ours", "helper"),
        [
            (date(2026, 9, 16), date(2026, 9, 18), date(2026, 9, 21)),
            (date(2026, 1, 24), date(2026, 1, 27), date(2026, 1, 28)),
            (date(2026, 1, 1), date(2026, 1, 5), date(2026, 1, 6)),
            (date(2026, 11, 26), date(2026, 11, 30), date(2026, 12, 1)),
        ],
    )
    def test_a_holiday_trade_date_counts_from_itself(self, trade, ours, helper):
        assert not USDMXN.is_settlement_day(trade)
        assert USDMXN.spot_date(trade) == ours
        assert helper > ours


class TestTheFederalReserveCalendar:
    """The dollar leg settles on Fedwire's days, held to QuantLib's
    ``UnitedStates(FederalReserve)``."""

    #: Every weekday QuantLib 1.44's FederalReserve calendar closes, 2025-2028.
    QUANTLIB_HOLIDAYS_2025_2028 = (
        "2025-01-01 2025-01-20 2025-02-17 2025-05-26 2025-06-19 2025-07-04 2025-09-01 "
        "2025-10-13 2025-11-11 2025-11-27 2025-12-25 2026-01-01 2026-01-19 2026-02-16 "
        "2026-05-25 2026-06-19 2026-09-07 2026-10-12 2026-11-11 2026-11-26 2026-12-25 "
        "2027-01-01 2027-01-18 2027-02-15 2027-05-31 2027-07-05 2027-09-06 2027-10-11 "
        "2027-11-11 2027-11-25 2028-01-17 2028-02-21 2028-05-29 2028-06-19 2028-07-04 "
        "2028-09-04 2028-10-09 2028-11-23 2028-12-25"
    ).split()

    #: SHA-256 of the comma-joined ISO dates of every weekday QuantLib 1.44's
    #: FederalReserve calendar closes from 1978-01-01 to 2100-12-31 (1,222 days).
    QUANTLIB_1978_2100_SHA256 = "0e2e6989d7e7922006025ca4a5096a82eff68f1fb08353800ae998bf84ed3709"

    @staticmethod
    def _weekday_holidays(first: date, last: date) -> list[str]:
        out, day = [], first
        while day <= last:
            if day.weekday() < 5 and not FEDERAL_RESERVE.is_business_day(day):
                out.append(day.isoformat())
            day += timedelta(days=1)
        return out

    def test_2025_to_2028_is_quantlibs_list(self):
        assert self._weekday_holidays(date(2025, 1, 1), date(2028, 12, 31)) == (
            self.QUANTLIB_HOLIDAYS_2025_2028
        )

    def test_1978_to_2100_is_quantlibs_list(self):
        found = self._weekday_holidays(date(1978, 1, 1), date(2100, 12, 31))
        assert len(found) == 1222
        assert hashlib.sha256(",".join(found).encode()).hexdigest() == self.QUANTLIB_1978_2100_SHA256

    def test_memorial_day_is_the_last_monday_even_on_the_31st(self):
        assert not FEDERAL_RESERVE.is_business_day(date(2027, 5, 31))
        assert FEDERAL_RESERVE.is_business_day(date(2027, 5, 24))

    def test_it_is_open_where_sifma_is_shut(self):
        """Good Friday, and the Friday before a Saturday 4 July."""
        for day in (date(2026, 4, 3), date(2026, 7, 3)):
            assert FEDERAL_RESERVE.is_business_day(day)
            assert not SIFMA_US.is_business_day(day)

    def test_a_sunday_holiday_is_the_monday_after(self):
        assert not FEDERAL_RESERVE.is_business_day(date(2022, 12, 26))
        assert not FEDERAL_RESERVE.is_business_day(date(2027, 7, 5))

    def test_quantlibs_settlement_calendar_would_have_put_1_july_on_the_6th(self):
        """Why the Federal Reserve's and not a calendar that closes the Friday
        before a Saturday holiday: QuantLib's FxSwapRateHelper with
        ``UnitedStates(Settlement)`` as the trading calendar gives 6 July
        2026, with the Federal Reserve 3 July. The peso exception does not
        change it: the 2nd and the 3rd are open in both."""
        assert USDMXN.spot_date(date(2026, 7, 1)) == date(2026, 7, 3)


class TestThePairCarriesItsSettlement:
    def test_calendars_default_from_the_currencies(self):
        built = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4)
        assert built.calendars == (
            SETTLEMENT_CALENDARS[Currency.USD], SETTLEMENT_CALENDARS[Currency.MXN]
        )
        assert built == USDMXN

    def test_every_currency_has_a_settlement_calendar(self):
        """A new currency fails here until it says where it settles."""
        assert set(SETTLEMENT_CALENDARS) == set(Currency)

    def test_the_dollar_settles_on_the_federal_reserve(self):
        assert SETTLEMENT_CALENDARS[Currency.USD] is FEDERAL_RESERVE

    def test_the_payload_names_the_lag_and_the_calendars(self):
        stored = USDMXN.to_dict()
        assert stored["spot_lag_days"] == 2
        assert stored["settlement_calendars"] == ["FEDERAL_RESERVE", "BMV"]
        assert stored["spot_lag_calendars"] == ["FEDERAL_RESERVE", "BMV"]

    @pytest.mark.parametrize("bad", [-1, 1.5, True])
    def test_a_lag_that_is_not_a_count_of_days_refuses(self, bad):
        with pytest.raises(ValueError, match="spot_lag_days"):
            CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=bad)


    def test_calendars_come_in_pairs(self):
        with pytest.raises(ValueError, match="base calendar, quote calendar"):
            CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, calendars=(BMV,))
