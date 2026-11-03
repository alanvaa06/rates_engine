"""Indices are data, and the evidence of a price names the right one.

The defect this file pins: every pricer wrote ``collateral_rate_ois_sofr``
into its evidence whatever the curve's currency, so a peso swap priced on a
peso curve reported an OIS-SOFR discount. The discounting fields now come
from :func:`rates_engine.conventions.indices.collateral_index`.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from test_sifma_publication_record import NO_SOFR_PUBLISHED, RECORD_FIRST, RECORD_LAST

from rates_engine.conventions.calendar import SIFMA_US, HolidayCalendar, SIFMAUSCalendar
from rates_engine.conventions.indices import (
    COLLATERAL_INDEX,
    INDICES,
    SOFR,
    TIIE_28,
    TIIE_FONDEO,
    UNRESOLVED_MXN,
    collateral_index,
)
from rates_engine.conventions.side import Side
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.evidence import DataQuality
from rates_engine.core.money import Currency
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.swaps import OISSwap
from rates_engine.pricing.linear import par_rate, pv
from rates_engine.risk.sensitivities import dv01

AS_OF = date(2026, 1, 15)

PUBLICATION_RECORDS = {
    "SIFMA_US": (RECORD_FIRST, RECORD_LAST, frozenset(NO_SOFR_PUBLISHED)),
}
"""The publication record each verified calendar is checked against, by calendar name.

The SIFMA one is the FRED transcription in ``tests/test_sifma_publication_record.py``:
first day, last day, and the weekdays with no SOFR between them.
"""


def _disagreements_with_record(calendar: HolidayCalendar) -> list[date]:
    """The weekdays of ``calendar``'s verified stretch on which it and its record part.

    A disagreement is a weekday the calendar calls a business day although
    the record has no publication for it, or a holiday although the record
    has one. The three preconditions are asserted first -- a record under
    the calendar's name, a declared verified stretch, a record that covers
    it -- so a calendar that misses one fails here instead of returning an
    empty list, and an empty list always means the loop ran over the whole
    stretch and found nothing.
    """
    assert calendar.name in PUBLICATION_RECORDS, calendar.name
    first, last, closed = PUBLICATION_RECORDS[calendar.name]
    verified_from = getattr(calendar, "VERIFIED_FROM", None)
    verified_through = getattr(calendar, "VERIFIED_THROUGH", None)
    assert verified_from is not None and verified_through is not None, calendar.name
    assert first <= verified_from < verified_through <= last, calendar.name
    day, disagreements = verified_from, []
    while day <= verified_through:
        if day.weekday() < 5 and calendar.is_business_day(day) == (day in closed):
            disagreements.append(day)
        day += timedelta(days=1)
    return disagreements


class _ThanksgivingADayLate(SIFMAUSCalendar):
    """``SIFMA_US`` with one holiday moved: Thanksgiving 2024 on the Friday.

    Thursday 28 November 2024 has no SOFR in the record and Friday the 29th
    has one, so this calendar disagrees with the record on exactly those two
    days and on no other. It inherits the verified stretch, which is the
    point: it declares everything ``SIFMA_US`` declares, and only the
    day-by-day check can tell the two apart.
    """

    name = "SIFMA_US_THANKSGIVING_2024_A_DAY_LATE"

    def holidays(self, year: int) -> frozenset[date]:
        days = super().holidays(year)
        if year != 2024:
            return days
        return (days - {date(2024, 11, 28)}) | {date(2024, 11, 29)}


def _curve(currency: Currency, rate: float = 0.05) -> DiscountCurve:
    nodes = tuple(AS_OF + timedelta(days=365 * k) for k in range(1, 6))
    dfs = tuple(math.exp(-rate * k) for k in range(1, 6))
    return DiscountCurve(AS_OF, nodes, dfs, currency=currency)


def _swap(index=SOFR) -> OISSwap:
    return OISSwap(
        effective=AS_OF + timedelta(days=30),
        maturity=AS_OF + timedelta(days=730),
        fixed_rate=0.05,
        notional=1_000_000.0,
        side=Side.PAYER,
        index=index,
    )


def _peso_swap() -> OISSwap:
    return _swap(TIIE_FONDEO)


class TestTheRegistry:
    def test_every_currency_says_what_it_discounts_at(self):
        """The extension point: a currency cannot exist without this entry."""
        missing = [c.value for c in Currency if c not in COLLATERAL_INDEX]
        assert missing == []

    def test_each_collateral_index_is_in_its_own_currency(self):
        for currency, index in COLLATERAL_INDEX.items():
            assert index.currency is currency

    def test_every_unresolved_name_is_explained(self):
        explained = {name for name, _ in UNRESOLVED_MXN}
        for index in (SOFR, TIIE_FONDEO, TIIE_28):
            assert set(index.unresolved) <= explained, index.name

    def test_sofr_is_verified_and_the_peso_indices_are_not(self):
        assert SOFR.is_verified
        assert not TIIE_FONDEO.is_verified
        assert not TIIE_28.is_verified

    def test_a_verified_index_fixes_on_a_calendar_that_agrees_with_a_record(self):
        """An index with no unresolved convention cannot sit on an unchecked calendar.

        Until v0.4 SOFR claimed ``is_verified`` while ``SIFMA_US`` recorded its
        Saturday rule as an unchecked assumption, and that rule was wrong.
        Declaring a verified stretch is not enough to pass here: the calendar
        must have a publication record in :data:`PUBLICATION_RECORDS`, the
        record must cover the stretch, and the calendar must agree with it on
        every weekday of the stretch. The two tests below make each half of
        that fail: a calendar with no record, and one with a record and a
        holiday moved.
        """
        checked = {
            index.name: _disagreements_with_record(index.calendar)
            for index in INDICES.values()
            if index.is_verified
        }
        assert "SOFR" in checked
        failing = {name: days[:5] for name, days in checked.items() if days}
        assert failing == {}, failing

    def test_a_calendar_with_dates_but_no_record_does_not_pass(self, monkeypatch):
        """A peso index made to claim verification, on a calendar that declares
        a verified stretch with no record behind it. It fails on the first
        precondition, before any day is compared; the next test is the one
        that reaches the comparison."""
        from dataclasses import replace

        from rates_engine.conventions.calendar import BMV

        monkeypatch.setattr(type(BMV), "VERIFIED_FROM", date(2018, 4, 3), raising=False)
        monkeypatch.setattr(type(BMV), "VERIFIED_THROUGH", date(2026, 10, 14), raising=False)
        with pytest.raises(AssertionError, match="BMV"):
            _disagreements_with_record(BMV)
        claimed = replace(TIIE_FONDEO, unresolved=())
        monkeypatch.setitem(INDICES, claimed.name, claimed)
        with pytest.raises(AssertionError):
            self.test_a_verified_index_fixes_on_a_calendar_that_agrees_with_a_record()

    def test_a_calendar_with_a_record_and_one_holiday_moved_does_not_pass(self, monkeypatch):
        """Review finding (round 3): the test above stops at its first assertion,
        so the day-by-day comparison never ran in a test that expects it to
        fail. Here every precondition holds -- the record is SIFMA's, the
        stretch is SIFMA's -- and only the comparison can find the two days
        on which Thanksgiving 2024 was moved. Any change to the loop that
        skips days, swaps the comparison or drops the weekday filter returns
        a different list."""
        from dataclasses import replace

        moved = _ThanksgivingADayLate()
        monkeypatch.setitem(PUBLICATION_RECORDS, moved.name, PUBLICATION_RECORDS["SIFMA_US"])
        assert _disagreements_with_record(moved) == [date(2024, 11, 28), date(2024, 11, 29)]
        assert _disagreements_with_record(SIFMA_US) == []
        claimed = replace(SOFR, name="SOFR_ON_A_MOVED_CALENDAR", calendar=moved)
        assert claimed.is_verified
        monkeypatch.setitem(INDICES, claimed.name, claimed)
        with pytest.raises(AssertionError, match="SOFR_ON_A_MOVED_CALENDAR"):
            self.test_a_verified_index_fixes_on_a_calendar_that_agrees_with_a_record()

    def test_sofr_s_calendar_covers_its_whole_published_history_so_far(self):
        assert SOFR.calendar is SIFMA_US
        assert SIFMA_US.VERIFIED_FROM == date(2018, 4, 3)
        assert SIFMA_US.VERIFIED_THROUGH >= date(2026, 10, 14)

    def test_the_peso_calendar_is_still_unverified_and_says_so(self):
        assert "mxn_calendar_is_bmv_not_banxico" in TIIE_FONDEO.unresolved
        assert "mxn_calendar_is_bmv_not_banxico" in TIIE_28.unresolved

    def test_a_currency_without_an_index_is_refused_rather_than_defaulted(self, monkeypatch):
        monkeypatch.delitem(COLLATERAL_INDEX, Currency.MXN)
        with pytest.raises(UnsupportedConventionError, match="MXN"):
            collateral_index(Currency.MXN)


class TestTheEvidenceNamesTheRightCurve:
    def test_a_dollar_price_is_unchanged(self):
        fields = pv(_swap(), CurveSet(_curve(Currency.USD))).evidence.fields
        assert fields["discounting"] == "collateral_rate_ois_sofr"
        assert fields["discounting_note"] == (
            "Discounted on the OIS-SOFR curve because collateral is remunerated at "
            "SOFR (Fujii-Shimada-Takahashi; Piterbarg), not on a separate funding curve."
        )

    def test_a_peso_price_no_longer_claims_sofr(self):
        fields = pv(_peso_swap(), CurveSet(_curve(Currency.MXN))).evidence.fields
        assert fields["discounting"] == "collateral_rate_ois_tiie_fondeo"
        assert "SOFR" not in fields["discounting_note"]
        assert "TIIE de Fondeo" in fields["discounting_note"]

    def test_the_swap_names_its_own_index(self):
        """The swap carries its index, so its evidence names it -- and for a
        dollar swap that is the v0.3 label, byte for byte."""
        peso = pv(_peso_swap(), CurveSet(_curve(Currency.MXN))).evidence.fields
        dollar = pv(_swap(), CurveSet(_curve(Currency.USD))).evidence.fields
        assert peso["instrument"]["float_index"] == "compounded_tiie_fondeo"
        assert dollar["instrument"]["float_index"] == "compounded_sofr"

    @pytest.mark.parametrize("measure", [pv, par_rate, dv01])
    def test_the_collateral_assumption_reaches_the_quality(self, measure):
        """A hand-built peso curve carries no provenance of its own; the
        assumption about peso collateral has to arrive anyway."""
        result = measure(_peso_swap(), CurveSet(_curve(Currency.MXN)))
        codes = {w.code for w in result.evidence.warnings}
        assert "unresolved_convention:mxn_collateral_rate" in codes
        assert result.evidence.worst_quality is DataQuality.ASSUMED

    def test_a_dollar_price_carries_no_collateral_assumption(self):
        result = pv(_swap(), CurveSet(_curve(Currency.USD)))
        assert result.evidence.warnings == ()
        assert result.evidence.worst_quality is DataQuality.OBSERVED

    def test_the_assumption_is_reported_once_when_the_curve_also_carries_it(self):
        from rates_engine.core.evidence import Degradation

        carried = Degradation(
            code="unresolved_convention:mxn_collateral_rate",
            message="from the curve",
            data_quality=DataQuality.ASSUMED,
        )
        base = _curve(Currency.MXN)
        curve = DiscountCurve(
            base.as_of, base.nodes, base.dfs, currency=Currency.MXN, provenance=(carried,)
        )
        warnings = pv(_peso_swap(), CurveSet(curve)).evidence.warnings
        matching = [w for w in warnings if w.code == carried.code]
        assert len(matching) == 1
        assert matching[0].message == "from the curve"

    def test_the_index_assumptions_reach_the_price(self):
        """A peso swap accrues and rolls on conventions this build could not
        verify. They are the index's, so they arrive even on a curve that
        carries no provenance of its own."""
        codes = {
            w.code
            for w in pv(_peso_swap(), CurveSet(_curve(Currency.MXN))).evidence.warnings
        }
        for name in TIIE_FONDEO.unresolved:
            assert f"unresolved_convention:{name}" in codes

    def test_a_dollar_swap_on_a_peso_curve_refuses(self):
        from rates_engine.core.errors import CurrencyMismatchError

        with pytest.raises(CurrencyMismatchError):
            pv(_swap(), CurveSet(_curve(Currency.MXN)))


class TestTheAssumptionReachesEveryMeasure:
    """Review finding: `pv` and `dv01` carried the peso collateral assumption
    but the key-rate profile, the curvature measures, the option pricers and
    the greeks did not, so a peso risk number read `observed`."""

    @staticmethod
    def _peso(curve):
        from dataclasses import replace

        return CurveSet(replace(curve, currency=Currency.MXN))

    @staticmethod
    def _assumed(result) -> None:
        codes = {w.code for w in result.evidence.warnings}
        assert "unresolved_convention:mxn_collateral_rate" in codes
        assert result.evidence.worst_quality is DataQuality.ASSUMED

    def test_linear_risk(self, option_curve):
        from rates_engine.risk.sensitivities import (
            effective_duration,
            key_rate_duration,
            key_rate_dv01,
            money_convexity,
        )

        curves, swap = self._peso(option_curve), _peso_swap()
        self._assumed(key_rate_dv01(swap, curves, (1.0, 2.0)))
        self._assumed(key_rate_duration(swap, curves, (1.0, 2.0)))
        self._assumed(money_convexity(swap, curves))
        self._assumed(effective_duration(swap, curves))

    def test_options_and_greeks(self, option_curve, atm_swaption):
        from dataclasses import replace

        from rates_engine.pricing.options import swaption_pv
        from rates_engine.risk.greeks import option_greeks
        from rates_engine.volatility.units import Volatility, VolUnits

        curves = self._peso(option_curve)
        vol = Volatility(90.0, VolUnits.NORMAL_BP)
        peso = replace(atm_swaption, underlying=replace(atm_swaption.underlying, index=TIIE_FONDEO))
        self._assumed(swaption_pv(peso, curves, vol))
        self._assumed(option_greeks(peso, curves, vol))

    def test_a_proxied_dollar_curve_reaches_the_risk_too(self, option_curve):
        """The same gap applied to any curve provenance, not just pesos."""
        from dataclasses import replace

        from rates_engine.core.evidence import Degradation
        from rates_engine.risk.sensitivities import key_rate_dv01

        proxy = Degradation("treasury_par_proxy", "long end proxied", DataQuality.PROXY)
        curves = CurveSet(replace(option_curve, provenance=(proxy,)))
        result = key_rate_dv01(_swap(), curves, (1.0, 2.0))
        assert result.evidence.worst_quality is DataQuality.PROXY


def test_a_curve_built_from_lists_cannot_go_stale():
    """Review finding: the cached arrays outlived a mutation of the lists a
    curve was built from. Inputs are now frozen into tuples."""
    nodes = [AS_OF + timedelta(days=365), AS_OF + timedelta(days=730)]
    dfs = [0.96, 0.92]
    curve = DiscountCurve(AS_OF, nodes, dfs)  # type: ignore[arg-type]
    before = curve.df(AS_OF + timedelta(days=500))
    dfs[1] = 0.5
    nodes.append(AS_OF + timedelta(days=1000))
    assert curve.df(AS_OF + timedelta(days=500)) == before
    assert len(curve.node_times) == len(curve.nodes) == 2
