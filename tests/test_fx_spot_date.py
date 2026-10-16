"""The spot date: a USD/MXN spot trade settles two joint business days later.

A quoted spot is the price for delivery on the spot date, not on the trade
date, and a forward's points run from there. Every expected date below was
produced by QuantLib 1.44, not by this package:

    cal = ql.JointCalendar(ql.UnitedStates(ql.UnitedStates.FederalReserve), ql.Mexico())
    cal.advance(trade_date, 2, ql.Days)

which is the calendar and the lag ``ql.FxSwapRateHelper`` uses with
``fixingDays=2``. The dates were chosen where the Federal Reserve, Settlement
and GovernmentBond US calendars all agree, so the comparison is not hostage
to the difference between ``SIFMA_US`` and a banking calendar that
``conventions/currency_pair.py`` documents.
"""

from __future__ import annotations

from datetime import date

import pytest

from rates_engine.conventions.calendar import BMV, SIFMA_US
from rates_engine.conventions.currency_pair import (
    SETTLEMENT_CALENDARS,
    USDMXN,
    CurrencyPair,
)
from rates_engine.core.money import Currency

QUANTLIB_SPOT_DATES = [
    # (trade date, QuantLib spot date, why it is interesting)
    (date(2026, 1, 15), date(2026, 1, 20), "Martin Luther King Day on T+2"),
    (date(2026, 1, 16), date(2026, 1, 21), "a Friday before a US Monday holiday"),
    (date(2026, 1, 17), date(2026, 1, 21), "a Saturday trade date"),
    (date(2026, 3, 31), date(2026, 4, 6), "Holy Thursday and Good Friday in Mexico"),
    (date(2026, 6, 17), date(2026, 6, 22), "Juneteenth on T+2"),
    (date(2026, 9, 14), date(2026, 9, 17), "16 September in Mexico: T+3"),
    (date(2026, 9, 16), date(2026, 9, 18), "a trade struck on a Mexican holiday"),
    (date(2026, 10, 8), date(2026, 10, 13), "Columbus Day"),
    (date(2026, 10, 13), date(2026, 10, 15), "an ordinary Tuesday"),
    (date(2026, 11, 12), date(2026, 11, 17), "Revolution Day, third Monday of November"),
    (date(2026, 11, 25), date(2026, 11, 30), "Thanksgiving on T+1"),
    (date(2026, 12, 10), date(2026, 12, 14), "a weekend inside the lag"),
    (date(2026, 12, 23), date(2026, 12, 28), "Christmas on T+2"),
    (date(2027, 2, 3), date(2027, 2, 5), "an ordinary Wednesday"),
]


class TestTheSpotDate:
    @pytest.mark.parametrize(
        ("trade", "expected", "why"), QUANTLIB_SPOT_DATES, ids=[c[2] for c in QUANTLIB_SPOT_DATES]
    )
    def test_it_matches_quantlibs_joint_calendar(self, trade, expected, why):
        assert USDMXN.spot_date(trade) == expected, why

    def test_the_pair_states_its_lag_and_calendars(self):
        assert USDMXN.spot_lag_days == 2
        assert USDMXN.calendars == (SIFMA_US, BMV)

    def test_a_mexican_holiday_alone_moves_it(self):
        """16 September is open in New York and shut in Mexico City, so a
        calendar of either country alone gets this one wrong."""
        assert SIFMA_US.is_business_day(date(2026, 9, 16))
        assert not USDMXN.is_settlement_day(date(2026, 9, 16))
        assert SIFMA_US.add_business_days(date(2026, 9, 14), 2) == date(2026, 9, 16)
        assert USDMXN.spot_date(date(2026, 9, 14)) == date(2026, 9, 17)

    def test_a_us_holiday_alone_moves_it(self):
        assert BMV.is_business_day(date(2026, 1, 19))
        assert not USDMXN.is_settlement_day(date(2026, 1, 19))
        assert BMV.add_business_days(date(2026, 1, 15), 2) == date(2026, 1, 19)

    def test_a_zero_lag_rolls_the_trade_date_forward_and_no_further(self):
        """QuantLib's ``advance(d, 0, Days)`` is ``adjust(d, Following)``."""
        same_day = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=0)
        assert same_day.spot_date(date(2026, 10, 8)) == date(2026, 10, 8)
        assert same_day.spot_date(date(2026, 9, 16)) == date(2026, 9, 17)

    def test_one_day_is_one_joint_business_day(self):
        next_day = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=1)
        assert next_day.spot_date(date(2026, 9, 15)) == date(2026, 9, 17)


class TestThePairCarriesItsSettlement:
    def test_calendars_default_from_the_currencies(self):
        built = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4)
        assert built.calendars == (SETTLEMENT_CALENDARS[Currency.USD], SETTLEMENT_CALENDARS[Currency.MXN])
        assert built == USDMXN

    def test_every_currency_has_a_settlement_calendar(self):
        """A new currency fails here until it says where it settles."""
        assert set(SETTLEMENT_CALENDARS) == set(Currency)

    def test_the_payload_names_the_lag_and_the_calendars(self):
        stored = USDMXN.to_dict()
        assert stored["spot_lag_days"] == 2
        assert stored["settlement_calendars"] == ["SIFMA_US", "BMV"]

    @pytest.mark.parametrize("bad", [-1, 1.5, True])
    def test_a_lag_that_is_not_a_count_of_days_refuses(self, bad):
        with pytest.raises(ValueError, match="spot_lag_days"):
            CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=bad)
