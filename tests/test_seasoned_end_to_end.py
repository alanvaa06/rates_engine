"""Seasoned trades end to end: one fixing lag, risk and hedges with fixings, the CLI's block.

#12 taught ``pv`` to value a trade that has started: paid flows drop out, the
floating coupon already decided is read from published fixings, and without
them the price is refused. Everything built on ``pv`` was left refusing such
a trade, because none of it could pass the fixings on. This file holds the
pieces that needed every branch merged first:

- one ``fixing_lag_days`` on :class:`~rates_engine.conventions.indices.RateIndex`,
  read by the linear pricer and the caps alike, so an IRS, a FRA and a
  caplet on one index fix on one day;
- ``fixings=`` through the risk measures, the futures-strip hedge, the CLI and
  the MCP server;
- a forward that starts before the curve's valuation date refused, for
  every caller;
- the cross-branch comparisons: the seasoned swap against QuantLib at full
  precision now that the snapshot compounds by the New York Fed's rule,
  the FOMC step curve against the snapshot, and a swap whose start rolls
  back over a weekend.

References are independent of the code: QuantLib 1.44 numbers pasted as
constants with the set-up that produced them, and closed forms on flat
curves.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rates_engine.conventions.calendar import SIFMA_US
from rates_engine.conventions.daycount import DayCount
from rates_engine.conventions.indices import (
    SOFR,
    TERM_SOFR_1M,
    TERM_SOFR_3M,
    TERM_SOFR_6M,
    TERM_SOFR_12M,
    TIIE_28,
    TIIE_FONDEO,
    UNRESOLVED_MXN,
    IndexTenor,
    RateIndex,
)
from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.core.money import Currency
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.capfloor import CapFloor, Caplet
from rates_engine.instruments.fra import FRA
from rates_engine.market.snapshot import MarketSnapshot, Series
from rates_engine.pricing.linear import pv

AS_OF = date(2026, 1, 15)
NOTIONAL = 100_000_000.0


def _flat_curve(rate: float = 0.04, currency: Currency = Currency.USD) -> DiscountCurve:
    """Flat continuous ``rate`` on ACT/365F; log-linear on it is exact everywhere."""
    nodes = tuple(AS_OF + timedelta(days=365 * k) for k in range(1, 11))
    dfs = tuple(math.exp(-rate * k) for k in range(1, 11))
    return DiscountCurve(AS_OF, nodes, dfs, currency=currency)


def _df(day: date, rate: float = 0.04) -> float:
    return math.exp(-rate * (day - AS_OF).days / 365.0)


def _published(series_id: str, values: dict[date, float]) -> Series:
    days = tuple(sorted(values))
    return Series(
        series_id,
        days,
        tuple(values[d] for d in days),
        Provenance(source="test", series_id=series_id, instrument_kind="fixing"),
    )


def _fixings(*series: Series, as_of: date = AS_OF) -> MarketSnapshot:
    return MarketSnapshot(as_of=as_of, series={s.series_id: s for s in series})


# ---------------------------------------------------------------------------
# One fixing lag, on the index.
# ---------------------------------------------------------------------------


class TestOneFixingLagOnTheIndex:
    """The lag is a property of the index, read once by every product that fixes on it.

    Before #21 the linear pricer kept a private constant for Term SOFR and
    the cap module another, applied to whatever index a cap named; the two
    could disagree, and an IRS and a caplet on one period could fix on
    different days.
    """

    @pytest.mark.parametrize("index", [TERM_SOFR_1M, TERM_SOFR_3M, TERM_SOFR_6M, TERM_SOFR_12M])
    def test_term_sofr_fixes_two_business_days_before_its_period(self, index):
        # The ARRC's Term SOFR conventions: set two US Government Securities
        # business days before the interest period.
        assert index.fixing_lag_days == 2

    @pytest.mark.parametrize("index", [SOFR, TIIE_FONDEO])
    def test_an_overnight_index_compounds_in_arrears_with_no_lag(self, index):
        assert index.fixing_lag_days == 0

    def test_tiie_28_reads_its_start_and_says_that_is_unverified(self):
        assert TIIE_28.fixing_lag_days == 0
        assert "tiie_28_fixing_lag" in TIIE_28.unresolved
        assert "tiie_28_fixing_lag" in {name for name, _ in UNRESOLVED_MXN}

    def test_a_tiie_28_fra_carries_the_assumption_as_a_degradation(self):
        fra = FRA(start=date(2026, 3, 2), end=date(2026, 3, 30), rate=0.075,
                  notional=1_000_000_000.0, index=TIIE_28)
        result = pv(fra, CurveSet(_flat_curve(0.07, Currency.MXN)))
        codes = {w.code: w for w in result.evidence.warnings}
        assert codes["unresolved_convention:tiie_28_fixing_lag"].data_quality is (
            DataQuality.ASSUMED
        )

    @pytest.mark.parametrize(
        ("benchmark", "stamped"), [("TIIE_FONDEO", False), ("TIIE_28", True)]
    )
    def test_only_a_tiie_28_curve_carries_the_lag(self, benchmark, stamped):
        # Review 3, point 8: adding the convention to UNRESOLVED_MXN stamped
        # it on every peso curve, TIIE de Fondeo ones included, which set no
        # term rate in advance.
        from rates_engine.curves.bootstrap import RealizedStubNode
        from rates_engine.curves.mxn import TIIEBenchmark, bootstrap_mxn_curve

        stub = RealizedStubNode(end=date(2026, 2, 12), accrual_factor=1.0 + 0.07 * 28 / 360)
        result = bootstrap_mxn_curve(AS_OF, (stub,), TIIEBenchmark[benchmark])
        code = "unresolved_convention:tiie_28_fixing_lag"
        assert (code in {w.code for w in result.evidence.warnings}) is stamped
        assert (code in {w.code for w in result.curve.provenance}) is stamped
        assert ("tiie_28_fixing_lag" in result.unresolved_conventions) is stamped
        # A Fondeo curve still carries every other peso assumption.
        assert result.evidence.worst_quality is DataQuality.ASSUMED

    def test_strict_conventions_describes_the_lag_on_tiie_28_only(self):
        from rates_engine.core.errors import UnresolvedConventionError
        from rates_engine.curves.mxn import TIIEBenchmark, bootstrap_mxn_curve

        with pytest.raises(UnresolvedConventionError) as caught:
            bootstrap_mxn_curve(AS_OF, (), TIIEBenchmark.TIIE_28, strict_conventions=True)
        assert "TIIE 28's fixing lag" in str(caught.value)
        with pytest.raises(UnresolvedConventionError) as caught:
            bootstrap_mxn_curve(AS_OF, (), TIIEBenchmark.TIIE_FONDEO, strict_conventions=True)
        assert "tiie_28_fixing_lag" not in str(caught.value)
        assert "fixing lag" not in str(caught.value)

    def test_an_irs_a_fra_and_a_caplet_on_one_period_fix_on_one_day(self):
        # The period from Tuesday 2026-01-20, the day after Martin Luther King
        # Day: two SIFMA business days back is Thursday the 15th, as_of.
        start, end = date(2026, 1, 20), date(2026, 4, 20)
        snapshot = _fixings(_published("TERM_SOFR_3M", {AS_OF: 0.0405}))
        fra = FRA(start=start, end=end, rate=0.04, notional=NOTIONAL)
        record = pv(fra, CurveSet(_flat_curve()), fixings=snapshot).evidence.fields[
            "realised_fixings"
        ][0]
        caplet = Caplet(accrual_start=start, accrual_end=end, payment=end, strike=0.04,
                        notional=NOTIONAL, kind=OptionKind.CALL)
        assert record["observed_from"] == caplet.fixing_date.isoformat() == "2026-01-15"

    def test_an_index_with_another_lag_moves_both_products_together(self):
        one_day = RateIndex(name="TEST_1D", label="Test one day", slug="test_1d",
                            currency=Currency.USD, tenor=IndexTenor.TERM_3M,
                            day_count=DayCount.ACT_360, calendar=SIFMA_US,
                            administrator="nobody", fixing_lag_days=1)
        # Friday 2026-01-16 less one business day is as_of, Thursday the 15th.
        start, end = date(2026, 1, 16), date(2026, 4, 16)
        snapshot = _fixings(_published("TEST_1D", {AS_OF: 0.0405}))
        fra = FRA(start=start, end=end, rate=0.04, notional=NOTIONAL, index=one_day)
        record = pv(fra, CurveSet(_flat_curve()), fixings=snapshot).evidence.fields[
            "realised_fixings"
        ][0]
        cap = CapFloor(effective=start, maturity=date(2027, 1, 16), strike=0.04, index=one_day)
        assert cap.fixing_lag_days == 1
        assert record["observed_from"] == cap.caplets[0].fixing_date.isoformat() == "2026-01-15"

    @staticmethod
    def _one_day(lag) -> RateIndex:
        return RateIndex(name="TEST_LAG", label="Test lag", slug="test_lag",
                         currency=Currency.USD, tenor=IndexTenor.TERM_3M,
                         day_count=DayCount.ACT_360, calendar=SIFMA_US,
                         administrator="nobody", fixing_lag_days=lag)

    @pytest.mark.parametrize("lag", ["int64", "int32", "uint8"])
    def test_a_numpy_integer_lag_is_a_whole_number_stored_as_an_int(self, lag):
        # #16's rule for a cap's lag, on the index too: RateIndex refused
        # np.int64(2), which a cap built on it accepted.
        import numpy as np

        index = self._one_day(getattr(np, lag)(2))
        assert type(index.fixing_lag_days) is int and index.fixing_lag_days == 2
        # Two SIFMA business days before Tuesday 2026-01-20 is Thursday the
        # 15th, across Martin Luther King Day; np.uint8(2) negated is 254.
        cap = CapFloor(effective=date(2026, 1, 20), maturity=date(2027, 1, 20), strike=0.04,
                       index=index)
        assert cap.caplets[0].fixing_date == date(2026, 1, 15)

    @pytest.mark.parametrize("lag", [True, 2.0, -1, "2"])
    def test_anything_but_a_whole_number_of_days_is_refused(self, lag):
        import numpy as np

        from rates_engine.core.errors import UnsupportedConventionError

        with pytest.raises(UnsupportedConventionError, match="whole number of business days"):
            self._one_day(lag)
        with pytest.raises(UnsupportedConventionError):
            self._one_day(np.True_)

    def test_a_cap_takes_its_lag_from_the_index_unless_told_otherwise(self):
        cap = CapFloor(effective=date(2026, 4, 20), maturity=date(2027, 4, 20), strike=0.04)
        assert cap.fixing_lag_days == TERM_SOFR_3M.fixing_lag_days == 2
        assert all(caplet.fixing_lag_days == 2 for caplet in cap.caplets)
        overridden = CapFloor(effective=date(2026, 4, 20), maturity=date(2027, 4, 20),
                              strike=0.04, fixing_lag_days=0)
        assert overridden.caplets[0].fixing_date == date(2026, 4, 20)
        # Resolved into the field, so the two spellings of one cap are equal.
        explicit = CapFloor(effective=date(2026, 4, 20), maturity=date(2027, 4, 20),
                            strike=0.04, fixing_lag_days=2)
        assert cap == explicit and hash(cap) == hash(explicit)


# ---------------------------------------------------------------------------
# A forward that starts before the curve does is refused.
# ---------------------------------------------------------------------------


class TestAForwardBeforeTheValuationDateIsRefused:
    """``DiscountCurve.df`` is one on or before ``as_of``, so such a forward is not anybody's rate.

    The part before ``as_of`` is history, published fixings, which a curve
    does not hold. The forward used to divide one by a discount factor and
    return the result: from 2025-12-15 to 2026-03-16 on a flat 4% curve
    that is 2.61% where the market rate is 4%, the cap error the review of
    2026-10-15 found. The pricers no longer ask; this makes sure no caller
    can.
    """

    def test_it_raises_and_names_the_way_round(self):
        from rates_engine.core.errors import MissingFixingError

        with pytest.raises(MissingFixingError) as caught:
            _flat_curve().forward(date(2025, 12, 15), date(2026, 3, 16))
        message = str(caught.value)
        assert "2025-12-15" in message and "2026-01-15" in message
        assert "fixings=" in message and "RealizedStubNode" in message

    def test_a_forward_from_the_valuation_date_itself_is_the_curve_s(self):
        end = date(2026, 4, 15)
        expected = (1.0 / _df(end) - 1.0) / ((end - AS_OF).days / 360.0)
        assert _flat_curve().forward(AS_OF, end) == pytest.approx(expected, rel=1e-13)

    def test_a_futures_node_that_has_started_cannot_calibrate_a_curve(self):
        # Its settlement compounds fixings from before as_of, which the
        # bootstrap would have read as accruing from as_of. The realised
        # part goes in a RealizedStubNode instead.
        from rates_engine.core.errors import MissingFixingError
        from rates_engine.curves.bootstrap import FuturesNode, bootstrap_discount_curve

        started = FuturesNode(start=date(2025, 12, 17), end=date(2026, 3, 18), forward_rate=0.04)
        with pytest.raises(MissingFixingError, match="RealizedStubNode"):
            bootstrap_discount_curve(AS_OF, (started,))


# ---------------------------------------------------------------------------
# Risk and hedges take the fixings, and the history stays put under a bump.
# ---------------------------------------------------------------------------

_SPOT = date(2026, 1, 20)
#: The OIS strip of the review's repro scripts (``curves/common.py``):
#: label, months from spot, par rate as a decimal; annual ACT/360, lag two.
_OIS_QUOTES = (
    ("1M", 1, 0.0431), ("3M", 3, 0.0428), ("6M", 6, 0.0419), ("1Y", 12, 0.0402),
    ("2Y", 24, 0.0375), ("3Y", 36, 0.0362), ("5Y", 60, 0.0358), ("7Y", 84, 0.0363),
    ("10Y", 120, 0.0372), ("15Y", 180, 0.0384), ("20Y", 240, 0.0389), ("30Y", 360, 0.0382),
)


@pytest.fixture(scope="module")
def strip_curve() -> DiscountCurve:
    """The engine's bootstrap of :data:`_OIS_QUOTES` with ``SwapQuoteNode``."""
    from rates_engine.conventions.schedule import add_months
    from rates_engine.curves.bootstrap import bootstrap_discount_curve
    from rates_engine.instruments.swaps import OISSwap
    from rates_engine.pricing.calibration import SwapQuoteNode

    nodes = tuple(
        SwapQuoteNode(
            swap=OISSwap(effective=_SPOT, maturity=add_months(_SPOT, months), fixed_rate=quote),
            quoted_rate=quote,
            label=label,
        )
        for label, months, quote in _OIS_QUOTES
    )
    return bootstrap_discount_curve(AS_OF, nodes).curve


def _sofr_history(rate: float = 0.0430) -> Series:
    days = []
    day = date(2024, 1, 1)
    while day < AS_OF:
        if SIFMA_US.is_business_day(day):
            days.append(day)
        day += timedelta(days=1)
    return Series(
        "SOFR", tuple(days), tuple(rate for _ in days), Provenance(source="test", series_id="SOFR")
    )


def _inputs(evidence) -> list:
    """Every provenance anywhere in an evidence chain."""
    found = list(evidence.inputs)
    for source in evidence.sources:
        found.extend(_inputs(source))
    return found


class TestRiskTakesTheFixings:
    """Every bump-and-reprice measure prices the trade ``pv`` prices.

    Before #21 none of them took ``fixings``: a seasoned swap was refused by
    every one, and a spot Term SOFR swap whose first period fixed on as_of
    had its DV01 measured on the forecast, not on the fixing ``pv`` read:
    -19,666.17 against -17,203.37 USD per basis point on the review's strip.
    """

    def test_the_dv01_of_the_review_repro_is_the_one_pv_implies(self, strip_curve):
        from rates_engine.instruments.swaps import IRSwap
        from rates_engine.pricing.linear import discounted_value
        from rates_engine.risk.bumps import shifted
        from rates_engine.risk.sensitivities import dv01

        irs = IRSwap(effective=_SPOT, maturity=date(2028, 1, 20), fixed_rate=0.039,
                     notional=NOTIONAL)
        curve_set = CurveSet(strip_curve)
        snapshot = _fixings(_published("TERM_SOFR_3M", {AS_OF: 0.0405}))
        up = discounted_value(irs, shifted(curve_set, 1e-4), fixings=snapshot)
        down = discounted_value(irs, shifted(curve_set, -1e-4), fixings=snapshot)
        measured = dv01(irs, curve_set, fixings=snapshot).value
        assert measured == (down - up) / 2.0
        # Regression pins only, from this implementation on the engine's own
        # bootstrap; the independent references are the closed form below
        # and the QuantLib constants of TestTheSeasonedHedgeAgainstQuantLib.
        assert measured == pytest.approx(-17_203.37, abs=0.01)
        assert dv01(irs, curve_set).value == pytest.approx(-19_666.17, abs=0.01)

    @pytest.mark.parametrize("fixing", [None, 0.0405])
    def test_a_spot_irs_dv01_reads_today_s_fixing_in_closed_form(self, fixing):
        # On a flat continuous 4% curve, P_y(t) = exp(-(0.04 + y) t) with t
        # in ACT/365F days. The floating leg is N (P(s) - P(e)) per forecast
        # period and N tau K0 P(e0) for the first, whose Term SOFR fixes on
        # as_of (two SIFMA days before 2026-01-20, across MLK Day); the fixed
        # leg N K tau P(e), annual ACT/360. The dates are the IRS's schedule,
        # every one a SIFMA business day.
        from rates_engine.instruments.swaps import IRSwap
        from rates_engine.risk.sensitivities import dv01

        floating = [date(2026, 1, 20), date(2026, 4, 20), date(2026, 7, 20), date(2026, 10, 20),
                    date(2027, 1, 20), date(2027, 4, 20), date(2027, 7, 20), date(2027, 10, 20),
                    date(2028, 1, 20)]
        fixed = [date(2026, 1, 20), date(2027, 1, 20), date(2028, 1, 20)]

        def value(y: float) -> float:
            def p(day: date) -> float:
                return math.exp(-(0.04 + y) * (day - AS_OF).days / 365.0)

            first, second = floating[0], floating[1]
            pairs = list(zip(floating[:-1], floating[1:], strict=True))
            if fixing is None:
                leg = sum(p(s) - p(e) for s, e in pairs)
            else:
                leg = sum(p(s) - p(e) for s, e in pairs[1:])
                leg += fixing * (second - first).days / 360.0 * p(second)
            annuity = sum(
                (e - s).days / 360.0 * p(e) for s, e in zip(fixed[:-1], fixed[1:], strict=True)
            )
            return NOTIONAL * (leg - 0.039 * annuity)

        irs = IRSwap(effective=_SPOT, maturity=date(2028, 1, 20), fixed_rate=0.039,
                     notional=NOTIONAL)
        snapshot = (
            None if fixing is None else _fixings(_published("TERM_SOFR_3M", {AS_OF: fixing}))
        )
        expected = (value(-1e-4) - value(1e-4)) / 2.0
        measured = dv01(irs, CurveSet(_flat_curve()), fixings=snapshot).value
        assert measured == pytest.approx(expected, rel=1e-10)

    def test_a_coupon_already_fixed_does_not_move_under_a_bump(self):
        # A FRA that fixed at 4.25% on 2025-12-11: its settlement is a known
        # amount, so only its discounting moves. On a flat curve the central
        # difference of A exp(-(r + y) t) is A exp(-r t) sinh(h t) per bump.
        from rates_engine.risk.sensitivities import dv01, money_convexity

        fra = FRA(start=date(2025, 12, 15), end=date(2026, 3, 16), rate=0.04, notional=NOTIONAL)
        snapshot = _fixings(_published("TERM_SOFR_3M", {date(2025, 12, 11): 0.0425}))
        amount = NOTIONAL * (0.0425 - 0.04) * 91 / 360
        t = (date(2026, 3, 16) - AS_OF).days / 365.0
        expected = amount * math.exp(-0.04 * t) * math.sinh(1e-4 * t)
        curve_set = CurveSet(_flat_curve())
        assert dv01(fra, curve_set, fixings=snapshot).value == pytest.approx(expected, rel=1e-9)
        convexity = money_convexity(fra, curve_set, fixings=snapshot).value
        second = amount * math.exp(-0.04 * t) * 2.0 * (math.cosh(1e-4 * t) - 1.0) / 1e-8
        assert convexity == pytest.approx(second, rel=1e-6)

    def test_the_realised_overnight_part_stays_fixed_and_the_rest_moves(self):
        # Period 2026-01-12 to 2027-01-12, paid 2027-01-14: three published
        # fixings, then the curve. Float leg PV(y) = N (R exp((r+y) T) - 1)
        # exp(-(r+y) P), fixed leg -N K tau exp(-(r+y) P).
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.risk.sensitivities import dv01

        published = {date(2026, 1, 12): 0.0431, date(2026, 1, 13): 0.0429,
                     date(2026, 1, 14): 0.0433}
        swap = OISSwap(effective=date(2026, 1, 12), maturity=date(2027, 1, 12), fixed_rate=0.04,
                       notional=NOTIONAL)
        realised = math.prod(1.0 + r / 360.0 for r in published.values())
        big_t = (date(2027, 1, 12) - AS_OF).days / 365.0
        pay_t = (date(2027, 1, 14) - AS_OF).days / 365.0

        def value(y: float) -> float:
            rate = 0.04 + y
            floating = NOTIONAL * (realised * math.exp(rate * big_t) - 1.0)
            return (floating - NOTIONAL * 0.04 * 365 / 360) * math.exp(-rate * pay_t)

        expected = (value(-1e-4) - value(1e-4)) / 2.0
        snapshot = _fixings(_published("SOFR", published))
        measured = dv01(swap, CurveSet(_flat_curve()), fixings=snapshot).value
        assert measured == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize(
        "name",
        ["dv01", "pvbp", "money_duration", "money_convexity", "effective_duration",
         "effective_convexity"],
    )
    def test_every_parallel_measure_prices_a_seasoned_swap(self, strip_curve, name):
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.risk import sensitivities

        swap = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                       notional=NOTIONAL)
        snapshot = _fixings(_sofr_history())
        result = getattr(sensitivities, name)(swap, CurveSet(strip_curve), fixings=snapshot)
        assert math.isfinite(result.value)
        assert "SOFR" in {p.series_id for p in _inputs(result.evidence)}

    @pytest.mark.parametrize("name", ["key_rate_dv01", "key_rate_duration"])
    def test_the_key_rate_profiles_price_it_and_still_sum_to_the_parallel(
        self, strip_curve, name
    ):
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.risk import sensitivities

        swap = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                       notional=NOTIONAL)
        curve_set = CurveSet(strip_curve)
        snapshot = _fixings(_sofr_history())
        profile = getattr(sensitivities, name)(swap, curve_set, (0.5, 1.0, 2.0, 5.0),
                                               fixings=snapshot)
        parallel = (
            sensitivities.dv01(swap, curve_set, fixings=snapshot).value
            if name == "key_rate_dv01"
            else sensitivities.effective_duration(swap, curve_set, fixings=snapshot).value
        )
        assert profile.total == pytest.approx(parallel, rel=1e-6)
        assert "SOFR" in {p.series_id for p in _inputs(profile.evidence)}

    def test_without_fixings_a_risk_measure_refuses_and_says_how_to_pass_them(self, strip_curve):
        from rates_engine.core.errors import MissingFixingError
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.risk.sensitivities import dv01

        swap = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04)
        with pytest.raises(MissingFixingError) as caught:
            dv01(swap, CurveSet(strip_curve))
        assert "fixings=" in str(caught.value) and "risk measures" in str(caught.value)

    def test_a_forecast_of_the_fixing_of_as_of_is_recorded_not_degraded(self, strip_curve):
        # Decision A of the round-3 reviews: a fixing dated as_of that is not
        # out yet is forecast, as QuantLib does by default, and recorded in
        # fixings_dated_as_of, not degraded. A spot IRS priced before the
        # print reads the same in its price, its par rate and its DV01 --
        # #12's point 3, which only the whole stack can check.
        from rates_engine.instruments.swaps import IRSwap
        from rates_engine.pricing.linear import par_rate
        from rates_engine.risk.sensitivities import dv01

        irs = IRSwap(effective=_SPOT, maturity=date(2028, 1, 20), fixed_rate=0.039,
                     notional=NOTIONAL)
        curve_set = CurveSet(strip_curve)
        results = (pv(irs, curve_set), par_rate(irs, curve_set), dv01(irs, curve_set))
        for result in results:
            assert result.evidence.worst_quality is DataQuality.OBSERVED
            assert result.evidence.warnings == results[0].evidence.warnings
        history = [s for s in results[2].evidence.sources if s.produced_by == "pricing.history"]
        assert [entry["source"] for entry in history[0].fields["fixings_dated_as_of"]] == [
            "curve"
        ]

        snapshot = _fixings(_published("TERM_SOFR_3M", {AS_OF: 0.0405}))
        read = dv01(irs, curve_set, fixings=snapshot)
        assert read.evidence.worst_quality is DataQuality.OBSERVED
        chained = [s for s in read.evidence.sources if s.produced_by == "pricing.history"]
        assert chained[0].fields["fixings_dated_as_of"][0]["source"] == "fixings"
        assert chained[0].fields["realised_fixings"][0]["rate"] == 0.0405
        assert "TERM_SOFR_3M" in {p.series_id for p in _inputs(read.evidence)}


class TestTheHistoryCostsNoProjectionOfItsOwn:
    """A measure records the history from a reprice it makes anyway.

    The first cut projected the whole trade once more inside every measure
    of a trade that could have history, which is every spot trade: a spot
    ten-year OIS's DV01 went from 0.192 to 0.311 ms on the review's machine,
    a thirty-year quarterly IRS's from 1.42 to 2.38 ms, and ``strip_hedge``
    recorded it twice. The history is the same on every moved curve, so
    counting projections pins the fix; ``benchmarks/bench_core.py`` times it.
    """

    @staticmethod
    def _counting(monkeypatch) -> list[int]:
        from rates_engine.pricing import linear

        calls = [0]
        project = linear.project

        def counted(*args, **kwargs):
            calls[0] += 1
            return project(*args, **kwargs)

        monkeypatch.setattr(linear, "project", counted)
        return calls

    def test_a_dv01_projects_the_trade_twice_and_still_records_its_history(
        self, monkeypatch, strip_curve
    ):
        from rates_engine.instruments.swaps import IRSwap
        from rates_engine.risk.sensitivities import dv01, key_rate_dv01, money_convexity

        irs = IRSwap(effective=_SPOT, maturity=date(2046, 1, 22), fixed_rate=0.039,
                     notional=NOTIONAL)
        curve_set = CurveSet(strip_curve)
        calls = self._counting(monkeypatch)
        result = dv01(irs, curve_set)
        assert calls[0] == 2
        assert any(s.produced_by == "pricing.history" for s in result.evidence.sources)
        calls[0] = 0
        money_convexity(irs, curve_set)
        assert calls[0] == 3
        calls[0] = 0
        key_rate_dv01(irs, curve_set, (1.0, 5.0, 10.0))
        assert calls[0] == 6

    def test_strip_hedge_records_it_once_through_its_dv01(self, monkeypatch):
        from rates_engine.hedging.futures_strip import strip_hedge
        from rates_engine.instruments.swaps import OISSwap

        nodes, start, end = TestTheStripHedgeTakesTheFixings._strip()
        swap = OISSwap(effective=date(2025, 7, 15), maturity=end, fixed_rate=0.04,
                       notional=NOTIONAL)
        calls = self._counting(monkeypatch)
        hedge = strip_hedge(swap, nodes, as_of=AS_OF, fixings=_fixings(_sofr_history()))
        futures = len(nodes) - 1
        # Two for the DV01, two per contract for its quote bumps, two for the
        # quote parallel: nothing for the record.
        assert calls[0] == 2 + 2 * futures + 2
        history = [s for s in hedge.evidence.sources if s.produced_by == "pricing.history"]
        assert len(history) == 1 and history[0].fields["realised_fixings"]


class TestTheSeasonedHedgeAgainstQuantLib:
    """The hedge and the shock table of a swap with history, against QuantLib 1.44.

    Review 3, point 7: the seasoned hedge tests checked only finiteness and
    row counts. These are QuantLib's numbers, computed in the review's
    scratch by ``ql_hedge_risk.py`` and pasted here:

    - curve: :meth:`TestTheStripHedgeTakesTheFixings._strip`, its node
      discount factors in closed form, ``P(e) = P(s) / (1 + 0.04 tau)``,
      in ``ql.DiscountCurve`` on Actual365Fixed (log-linear, as the
      engine's ``log_linear_df``);
    - swaps: ``ql.OvernightIndexedSwap`` payer, USD 100m, 4%, annual,
      ACT/360, payment lag 2 (Following), UnitedStates(SOFR), Modified
      Following, backward, to 2028-06-21. ``S`` from 2025-07-15 with
      ``ql.Sofr`` fixed at 4.30% on every business day to 2026-01-14;
      ``T`` from as_of, 2026-01-15, with only that day's 4.30%;
    - a parallel shock: ``ql.ZeroSpreadedTermStructure``, continuous on
      Actual365Fixed, on discounting and forecasting alike; a quote bump:
      one contract's forward moved 1 bp and the node factors rebuilt.

    ``T`` is the case a dropped snapshot cannot hide: without the fixing
    of as_of, nothing is refused, the day is forecast, and the post-shock
    DV01 at +25 bp is QuantLib's -23,424.14 instead of -23,396.75.
    """

    QUANTLIB = {
        "S": {
            "pv": 334864.0776166469,
            "dv01": -23652.776560653932,
            "money_convexity": -569333857.8566909,
            "swap_pnl": {-25.0: -593102.1729864124, 25.0: 589543.8254622538},
            "post_dv01": {-25.0: -23795.54181419965, 25.0: -23510.873136985116},
            "deltas": (-2549.130642056465, -2460.7170258909464, -2455.7049002051353,
                       -2455.704900204204, -2455.7049002051353, -2365.407432194799,
                       -2360.191495971754, -2360.1914959726855, -2539.7581305131316),
            "contracts": 880.1004369285703,
        },
        "T": {
            "pv": 122958.65465028025,
            "dv01": -23538.554601969197,
            "money_convexity": -568944211.3041878,
            "swap_pnl": {-25.0: -590245.4059357308, 25.0: 586689.4936993085},
            "post_dv01": {-25.0: -23681.222391138785, 25.0: -23396.748536942527},
            "deltas": (-2498.866500252858, -2458.071806671098, -2455.704900204204,
                       -2455.704900204204, -2455.704900204204, -2365.407432194799,
                       -2360.191495971754, -2360.191495971754, -2539.758130514063),
            "contracts": 877.9840624875575,
        },
    }
    #: QuantLib's post-shock DV01 of ``T`` at +25 bp without the fixing of as_of.
    T_FORECAST_POST_DV01 = -23424.13951762952

    @staticmethod
    def _case(name: str):
        from rates_engine.hedging.futures_strip import strip_hedge
        from rates_engine.instruments.swaps import OISSwap

        nodes, _, end = TestTheStripHedgeTakesTheFixings._strip()
        if name == "S":
            effective, snapshot = date(2025, 7, 15), _fixings(_sofr_history())
        else:
            effective, snapshot = AS_OF, _fixings(_published("SOFR", {AS_OF: 0.0430}))
        swap = OISSwap(effective=effective, maturity=end, fixed_rate=0.04, notional=NOTIONAL)
        return swap, snapshot, strip_hedge(swap, nodes, as_of=AS_OF, fixings=snapshot)

    @pytest.mark.parametrize("name", ["S", "T"])
    def test_the_hedge_is_sized_on_quantlib_s_deltas(self, name):
        reference = self.QUANTLIB[name]
        _, _, hedge = self._case(name)
        assert hedge.swap_dv01 == pytest.approx(reference["dv01"], rel=1e-9)
        deltas = tuple(hedge.bucketed_delta_by_instrument.values())
        assert deltas == pytest.approx(reference["deltas"], rel=1e-9)
        assert hedge.total_contracts == pytest.approx(reference["contracts"], rel=1e-9)

    @pytest.mark.parametrize("name", ["S", "T"])
    def test_the_risk_measures_are_quantlib_s(self, name):
        from rates_engine.risk.sensitivities import dv01, money_convexity

        reference = self.QUANTLIB[name]
        swap, snapshot, hedge = self._case(name)
        assert pv(swap, hedge.curve_set, fixings=snapshot).value == pytest.approx(
            reference["pv"], rel=1e-12
        )
        assert dv01(swap, hedge.curve_set, fixings=snapshot).value == pytest.approx(
            reference["dv01"], rel=1e-9
        )
        # A second difference at 1 bp: QuantLib's carries about 4e-9 of noise.
        assert money_convexity(swap, hedge.curve_set, fixings=snapshot).value == pytest.approx(
            reference["money_convexity"], rel=1e-7
        )

    @pytest.mark.parametrize("name", ["S", "T"])
    def test_the_shock_table_reprices_the_swap_with_its_fixings(self, name):
        # The swap's P&L and post-shock DV01 are QuantLib's; the strip's P&L
        # is the closed form of each contract's forward move on the shocked
        # curve, ((1 + f tau) exp(y (t_e - t_s)) - 1) / tau, at QuantLib's
        # contract counts (minus delta over the SR3 tick, 25 USD a bp).
        from rates_engine.hedging.futures_strip import shock_table

        reference = self.QUANTLIB[name]
        _, _, hedge = self._case(name)
        rows = shock_table(hedge, (-25.0, 25.0)).table.set_index("shock_bp")
        for shock in (-25.0, 25.0):
            row = rows.loc[shock]
            assert row["swap_pnl"] == pytest.approx(reference["swap_pnl"][shock], rel=1e-9)
            assert row["swap_dv01_post_shock"] == pytest.approx(
                reference["post_dv01"][shock], rel=1e-9
            )
            strip = 0.0
            for delta, (start, end) in zip(reference["deltas"], hedge.periods.values(),
                                           strict=True):
                tau = (end - start).days / 360.0
                years = (end - start).days / 365.0
                moved = ((1.0 + 0.04 * tau) * math.exp(shock * 1e-4 * years) - 1.0) / tau
                strip += delta * (moved - 0.04) * 1e4
            assert row["strip_pnl"] == pytest.approx(strip, rel=1e-8)
            assert row["net_pnl"] == pytest.approx(reference["swap_pnl"][shock] + strip, rel=1e-6)

    def test_a_shock_table_without_the_fixings_would_not_match(self):
        # What a regression dropping fixings from the post-shock DV01 would
        # give on T: no refusal, and a number 27.39 USD/bp off, a thousand
        # times the tolerance above.
        from rates_engine.hedging.futures_strip import shock_table

        _, _, hedge = self._case("T")
        post = shock_table(hedge, (25.0,)).table["swap_dv01_post_shock"].iloc[0]
        assert post == pytest.approx(self.QUANTLIB["T"]["post_dv01"][25.0], rel=1e-9)
        assert abs(post - self.T_FORECAST_POST_DV01) > 1e3 * 1e-9 * abs(post)


class TestTheStripHedgeTakesTheFixings:
    """A seasoned swap can be hedged: sized, and shocked, on the trade ``pv`` prices."""

    @staticmethod
    def _strip():
        from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
        from rates_engine.curves.bootstrap import FuturesNode, RealizedStubNode

        start = imm_date(2026, 3)
        nodes = [RealizedStubNode(end=start,
                                  accrual_factor=1.0 + 0.04 * ((start - AS_OF).days / 360.0))]
        period = start
        for index in range(9):
            following = next_imm_on_or_after(period + timedelta(days=1))
            nodes.append(FuturesNode(start=period, end=following, forward_rate=0.04,
                                     label=f"SR3-{index + 1}"))
            period = following
        return tuple(nodes), start, period

    def test_a_seasoned_swap_is_sized_and_shocked_with_its_fixings(self):
        from rates_engine.core.errors import MissingFixingError
        from rates_engine.hedging.futures_strip import shock_table, strip_hedge
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.risk.sensitivities import dv01

        nodes, start, end = self._strip()
        swap = OISSwap(effective=date(2025, 7, 15), maturity=end, fixed_rate=0.04,
                       notional=NOTIONAL)
        snapshot = _fixings(_sofr_history())
        with pytest.raises(MissingFixingError):
            strip_hedge(swap, nodes, as_of=AS_OF)
        hedge = strip_hedge(swap, nodes, as_of=AS_OF, fixings=snapshot)
        assert hedge.swap_dv01 == dv01(swap, hedge.curve_set, fixings=snapshot).value
        assert hedge.fixings is snapshot
        table = shock_table(hedge, (-25.0, 25.0))
        assert len(table.table) == 2
        assert "SOFR" in {p.series_id for p in _inputs(table.evidence)}

    def test_the_coverage_a_seasoned_swap_needs_starts_at_as_of(self):
        # Nothing before as_of has rate risk left. Without the stub the
        # refusal names as_of, not the trade date of a swap struck six
        # months ago.
        from rates_engine.core.errors import IncompleteStripError
        from rates_engine.hedging.futures_strip import strip_hedge
        from rates_engine.instruments.swaps import OISSwap

        nodes, start, end = self._strip()
        swap = OISSwap(effective=date(2025, 7, 15), maturity=end, fixed_rate=0.04,
                       notional=NOTIONAL)
        with pytest.raises(IncompleteStripError, match="2026-01-15") as caught:
            strip_hedge(swap, nodes[1:], as_of=AS_OF, fixings=_fixings(_sofr_history()))
        assert "2025-07-15" not in str(caught.value)

    def test_the_realised_stub_covers_as_of_to_its_end(self):
        # The review's repro: effective 2025-09-17, as of 2026-01-15, a stub
        # to the March IMM date, then SR3s. Under the default it raised
        # "the strip starts at 2026-03-18 but the swap starts at 2026-01-15",
        # and no config closed the gap: a contract that has started is
        # refused, and the stub is what stands in for it.
        from rates_engine.hedging.futures_strip import strip_hedge
        from rates_engine.instruments.swaps import OISSwap

        nodes, start, end = self._strip()
        swap = OISSwap(effective=date(2025, 9, 17), maturity=end, fixed_rate=0.04,
                       notional=NOTIONAL)
        hedge = strip_hedge(swap, nodes, as_of=AS_OF, fixings=_fixings(_sofr_history()))
        fields = hedge.evidence.fields
        assert fields["coverage_checked"] is True
        assert fields["coverage_realised_stub"] == [
            {"from": "2026-01-15", "to": start.isoformat()}
        ]
        assert fields["swap_span"][0] == "2026-01-15"
        assert fields["coverage_tolerated_gaps"] == []

    def test_a_stub_short_of_the_first_contract_leaves_a_hole(self):
        # The stub counts to its end and no further: a day between it and the
        # first contract is a day nobody holds.
        from rates_engine.core.errors import IncompleteStripError
        from rates_engine.curves.bootstrap import RealizedStubNode
        from rates_engine.hedging.futures_strip import strip_hedge
        from rates_engine.instruments.swaps import OISSwap

        nodes, start, end = self._strip()
        short = RealizedStubNode(end=start - timedelta(days=7), accrual_factor=1.006)
        swap = OISSwap(effective=date(2025, 9, 17), maturity=end, fixed_rate=0.04,
                       notional=NOTIONAL)
        with pytest.raises(IncompleteStripError, match=f"covering {start - timedelta(days=7)}"):
            strip_hedge(swap, (short, *nodes[1:]), as_of=AS_OF,
                        fixings=_fixings(_sofr_history()))


# ---------------------------------------------------------------------------
# The CLI and the MCP server read fixings from the config.
# ---------------------------------------------------------------------------


def _seasoned_config(*, fixings: bool = True, **sofr: object) -> dict:
    """A desk file for a payer struck on 2025-09-17, six months before as_of.

    The curve block is the suite's flat 4% futures strip. ``fixings``, when
    asked for, holds SOFR at 4.30% for every SIFMA business day the swap's
    first period has accrued over, the shape ``rateng describe`` documents.
    """
    from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after

    start = imm_date(2026, 3)
    futures = []
    period = start
    for index in range(9):
        following = next_imm_on_or_after(period + timedelta(days=1))
        futures.append({"label": f"SR3-{index + 1}", "start": period.isoformat(),
                        "end": following.isoformat(), "price": 96.0})
        period = following
    config: dict = {
        "as_of": AS_OF.isoformat(),
        "curve": {
            "stub": {"end": start.isoformat(),
                     "accrual_factor": 1.0 + 0.04 * ((start - AS_OF).days / 360.0)},
            "futures": futures,
        },
        "swap": {"effective": "2025-09-17", "maturity": period.isoformat(), "fixed_rate": 0.04,
                 "notional": NOTIONAL, "side": "payer"},
        "hedge": {"shocks_bp": [-25, 25]},
    }
    if fixings:
        days = [d for d in _sofr_history().dates if d >= date(2025, 9, 17)]
        config["fixings"] = {
            "SOFR": {"values": [{"date": d.isoformat(), "rate": 0.0430} for d in days], **sofr}
        }
    return config


def _library_price(config: dict) -> float:
    """The same swap, curve and history priced through the library itself."""
    from rates_engine.app.builders import calibration_nodes, ois_swap
    from rates_engine.app.config import as_date
    from rates_engine.curves.bootstrap import bootstrap_discount_curve

    as_of = as_date(config["as_of"])
    curve = bootstrap_discount_curve(as_of, calibration_nodes(config, as_of)).curve
    days = [as_date(entry["date"]) for entry in config["fixings"]["SOFR"]["values"]]
    series = Series("SOFR", tuple(days), tuple(0.0430 for _ in days),
                    Provenance(source="config", series_id="SOFR"))
    return pv(ois_swap(config), CurveSet(curve), fixings=_fixings(series)).value


class TestTheConfigCarriesTheFixings:
    """``price`` and ``hedge`` value a swap that has started when the file holds its history."""

    def test_without_them_the_refusal_names_the_config_key(self):
        from rates_engine.app import commands
        from rates_engine.core.errors import MissingFixingError

        for name in ("price", "hedge"):
            with pytest.raises(MissingFixingError) as caught:
                commands.COMMANDS[name](_seasoned_config(fixings=False))
            message = str(caught.value)
            assert "fixings.SOFR.values" in message, name
            assert "MarketSnapshot" not in message, name
            assert "2025-09-17" in message, name

    @pytest.mark.parametrize("name", ["bootstrap", "price", "hedge"])
    def test_a_started_futures_entry_is_named_and_not_sent_to_the_fixings_block(self, name):
        # Review 3, point 2: a curve.futures entry from 2025-12-17 to
        # 2026-03-18 as of 2026-01-15 used to come back as the swap's refusal
        # rewritten to "fixings.SOFR.values", which no curve node reads.
        from rates_engine.app import commands
        from rates_engine.core.errors import MissingFixingError

        config = _seasoned_config()
        del config["curve"]["stub"]
        config["curve"]["futures"].insert(
            0, {"label": "SR3-Z5", "start": "2025-12-17", "end": "2026-03-18", "price": 96.0}
        )
        with pytest.raises(MissingFixingError) as caught:
            commands.COMMANDS[name](config)
        message = str(caught.value)
        assert message.startswith("curve.futures[0] (SR3-Z5) runs from 2025-12-17")
        assert "curve.stub" in message and '"end": "2026-03-18"' in message
        assert "fixings.SOFR.values" not in message

    def test_a_par_swap_rolled_before_a_saturday_names_the_way_out_that_works(self):
        # As of Saturday 2026-01-31 a par swap effective that day rolls back
        # to Friday the 30th (Modified Following, month end). Adding Friday's
        # fixing, as the old message said, still failed: a SwapQuoteNode
        # takes no fixings. The message now says what does work, and doing
        # it prices.
        from rates_engine.app import commands
        from rates_engine.core.errors import MissingFixingError

        config = {
            "as_of": "2026-01-31",
            "curve": {"par_swaps": [
                {"label": "1Y", "effective": "2026-01-31", "maturity": "2027-02-01",
                 "rate": 0.04},
                {"label": "2Y", "effective": "2026-01-31", "maturity": "2028-01-31",
                 "rate": 0.04},
            ]},
            "swap": {"effective": "2026-02-03", "maturity": "2027-02-03", "fixed_rate": 0.04,
                     "notional": NOTIONAL},
            "fixings": {"SOFR": {"values": [{"date": "2026-01-30", "rate": 0.04}]}},
        }
        with pytest.raises(MissingFixingError) as caught:
            commands.COMMANDS["price"](config)
        message = str(caught.value)
        assert message.startswith("curve.par_swaps[0] (1Y) starts on 2026-01-30")
        assert "takes none" in message and "fixings.SOFR.values" not in message
        assert "2026-02-02" in message and "as of 2026-01-30" in message
        for entry in config["curve"]["par_swaps"]:
            entry["effective"] = "2026-02-02"
        assert math.isfinite(commands.COMMANDS["price"](config)["value"])

    def test_price_values_the_swap_on_the_history_the_file_holds(self):
        from rates_engine.app import commands

        config = _seasoned_config()
        payload = commands.COMMANDS["price"](config)
        assert payload["value"] == _library_price(config)
        records = payload["evidence"]["fields"]["realised_fixings"]
        assert records[0]["series_id"] == "SOFR" and records[0]["source"] == "config"
        assert math.isfinite(payload["measures"]["dv01"]["value"])
        dv01_sources = payload["measures"]["dv01"]["evidence"]["sources"]
        assert any(s["produced_by"] == "pricing.history" for s in dv01_sources)

    def test_hedge_sizes_it_and_shocks_it(self):
        from rates_engine.app import commands

        payload = commands.COMMANDS["hedge"](_seasoned_config())
        assert payload["total_contracts"] is not None
        assert len(payload["shock_table"]["rows"]) == 2

    def test_the_mcp_tool_serves_the_same_document(self):
        import json

        from rates_engine import mcp_server
        from rates_engine.app import commands

        config = _seasoned_config()
        served = json.loads(mcp_server.call_tool("price", config))
        assert served == json.loads(json.dumps(commands.COMMANDS["price"](config)))

    def test_the_cli_exits_1_and_points_at_the_block(self, tmp_path, capsys):
        import json

        from rates_engine.cli import main

        path = tmp_path / "seasoned.json"
        path.write_text(json.dumps(_seasoned_config(fixings=False)), encoding="utf-8")
        assert main(["price", "--config", str(path), "--json"]) == 1
        document = json.loads(capsys.readouterr().out)
        assert document["error"]["type"] == "MissingFixingError"
        assert "fixings.SOFR.values" in document["error"]["message"]

    def test_the_fixings_quality_reaches_the_price(self):
        from rates_engine.app import commands

        payload = commands.COMMANDS["price"](_seasoned_config(data_quality="assumed"))
        assert payload["evidence"]["data_quality"] == "assumed"

    def test_a_rate_in_percent_is_refused_by_its_path(self):
        from rates_engine.app import commands
        from rates_engine.core.errors import ImplausibleInputError

        config = _seasoned_config()
        config["fixings"]["SOFR"]["values"][3]["rate"] = 4.30
        with pytest.raises(ImplausibleInputError, match=r"^fixings\.SOFR\.values\[3\]\.rate"):
            commands.COMMANDS["price"](config)


class TestTheFixingsBlockIsCheckedByPath:
    """Review 3, points 3, 5 and 6: what the ``fixings`` block refuses, and where it says so."""

    @staticmethod
    def _at(curve_rate: float, fixing: float) -> dict:
        """The seasoned file with the curve at ``curve_rate`` and every fixing at ``fixing``."""
        config = _seasoned_config()
        stub = config["curve"]["stub"]
        tau = (date.fromisoformat(stub["end"]) - AS_OF).days / 360.0
        stub["accrual_factor"] = 1.0 + curve_rate * tau
        for entry in config["curve"]["futures"]:
            entry["price"] = 100.0 * (1.0 - curve_rate)
        for entry in config["fixings"]["SOFR"]["values"]:
            entry["rate"] = fixing
        return config

    @pytest.mark.parametrize("name", ["price", "hedge"])
    def test_a_tenfold_typo_is_refused(self, name):
        # Every SOFR entry typed 0.43 for 0.043 priced the payer at
        # 14,200,798.33 USD instead of 255,311.41, marked observed: the old
        # check refused only a magnitude of one or more.
        from rates_engine.app import commands
        from rates_engine.core.errors import ImplausibleInputError

        config = _seasoned_config()
        for entry in config["fixings"]["SOFR"]["values"]:
            entry["rate"] = 0.43
        with pytest.raises(ImplausibleInputError, match=r"^fixings\.SOFR\.values\[0\]\.rate"):
            commands.COMMANDS[name](config)

    @pytest.mark.parametrize("name", ["price", "hedge"])
    def test_a_tenfold_typo_inside_the_band_is_caught_against_the_curve(self, name):
        # 0.15 for 0.015 on a 1.5% curve: inside the 20% band, ten times
        # the curve's overnight rate.
        from rates_engine.app import commands
        from rates_engine.core.errors import ImplausibleInputError

        config = self._at(0.015, 0.15)
        last = len(config["fixings"]["SOFR"]["values"]) - 1
        with pytest.raises(ImplausibleInputError) as caught:
            commands.COMMANDS[name](config)
        message = str(caught.value)
        assert message.startswith(f"fixings.SOFR.values[{last}].rate, the 2026-01-14 fixing")
        assert "10 times off" in message

    def test_a_percent_file_from_the_years_near_zero_is_caught(self):
        # 0.05 meaning 0.05%, on a curve at 0.05%: read as 5%, a rate SOFR
        # has printed, so no band can see it; the curve is a hundred times off.
        from rates_engine.app import commands
        from rates_engine.core.errors import ImplausibleInputError

        with pytest.raises(ImplausibleInputError, match="100 times off"):
            commands.COMMANDS["price"](self._at(0.0005, 0.05))

    def test_the_market_s_worst_day_is_not_refused(self):
        # The repo spike of 17 September 2019: SOFR printed 5.25% against a
        # curve near 2.2%, 2.4 times off.
        from rates_engine.app import commands

        config = self._at(0.022, 0.022)
        config["fixings"]["SOFR"]["values"][-1]["rate"] = 0.0525
        assert math.isfinite(commands.COMMANDS["price"](config)["value"])

    def test_near_zero_noise_is_not_refused(self):
        # 0.01% against a 0.05% curve is five times off and nothing at all.
        from rates_engine.app import commands

        assert math.isfinite(commands.COMMANDS["price"](self._at(0.0005, 0.0001))["value"])

    def test_a_date_that_is_not_iso_is_refused_by_its_path(self):
        from rates_engine.app import commands
        from rates_engine.core.errors import ConfigurationError

        config = _seasoned_config()
        config["fixings"]["SOFR"]["values"][2]["date"] = "17/09/2025"
        with pytest.raises(ConfigurationError, match=r"^fixings\.SOFR\.values\[2\]\.date is "):
            commands.COMMANDS["price"](config)

    def test_an_unknown_data_quality_is_refused_by_its_path(self):
        from rates_engine.app import commands
        from rates_engine.core.errors import ConfigurationError

        config = _seasoned_config(data_quality="good")
        with pytest.raises(ConfigurationError) as caught:
            commands.COMMANDS["price"](config)
        assert str(caught.value) == (
            "fixings.SOFR.data_quality is 'good'; it must be one of observed, synthetic, "
            "proxy, assumed."
        )

    @pytest.mark.parametrize(
        ("day", "weekday"), [("2025-09-20", "Saturday"), ("2025-11-27", "Thursday")]
    )
    def test_a_row_on_a_closed_day_is_refused_not_ignored(self, day, weekday):
        # A Saturday and Thanksgiving: SIFMA is shut, no rate is published,
        # and the row used to be accepted and read by nothing.
        from rates_engine.app import commands
        from rates_engine.core.errors import ConfigurationError

        config = _seasoned_config()
        config["fixings"]["SOFR"]["values"][2]["date"] = day
        with pytest.raises(ConfigurationError) as caught:
            commands.COMMANDS["price"](config)
        message = str(caught.value)
        assert message.startswith(f"fixings.SOFR.values[2].date is {day}, a {weekday}")
        assert "SIFMA_US is closed" in message

    def test_an_entry_dated_as_of_is_read_and_decides_the_day(self):
        # The schema said the values run "to the day before as_of"; one
        # dated as_of is read, and decides as_of up to the next business day
        # (Friday the 16th), as the schema now says.
        from rates_engine.app import commands
        from rates_engine.app.config import CONFIG_SCHEMA

        config = _seasoned_config()
        without = commands.COMMANDS["price"](config)
        config["fixings"]["SOFR"]["values"].append({"date": AS_OF.isoformat(), "rate": 0.043})
        with_it = commands.COMMANDS["price"](config)
        assert without["evidence"]["fields"]["realised_fixings"][0]["observed_to"] == (
            "2026-01-15"
        )
        assert with_it["evidence"]["fields"]["realised_fixings"][0]["observed_to"] == (
            "2026-01-16"
        )
        text = CONFIG_SCHEMA["fixings"]["keys"]["SOFR"]["keys"]["values"]["description"]
        assert "day before as_of" not in text and "dated as_of" in text

    def test_a_date_given_twice_is_refused(self):
        from rates_engine.app import commands
        from rates_engine.core.errors import ConfigurationError

        config = _seasoned_config()
        values = config["fixings"]["SOFR"]["values"]
        values.append(dict(values[0]))
        with pytest.raises(ConfigurationError, match="2025-09-17"):
            commands.COMMANDS["price"](config)

    def test_a_misspelt_series_is_refused_with_the_name_it_meant(self):
        from rates_engine.app import commands
        from rates_engine.core.errors import ConfigurationError

        config = _seasoned_config()
        config["fixings"] = {"SOFRR": config["fixings"]["SOFR"]}
        with pytest.raises(ConfigurationError, match="did you mean SOFR"):
            commands.COMMANDS["price"](config)

    def test_a_swap_with_no_history_ignores_the_block(self):
        # The README's swap starts at the March IMM date; a file that also
        # carries SOFR history prices it exactly as without.
        from rates_engine.app import commands

        config = _seasoned_config()
        config["swap"]["effective"] = config["curve"]["stub"]["end"]
        bare = dict(config)
        bare.pop("fixings")
        assert commands.COMMANDS["price"](config) == commands.COMMANDS["price"](bare)


# ---------------------------------------------------------------------------
# Cross-branch checks: the FOMC curve against the snapshot, a weekend start.
# ---------------------------------------------------------------------------


class TestTheFomcCurveAndTheSnapshotCompoundAlike:
    """Two implementations of the New York Fed's rule, one for a path and one for a history.

    ``FOMCStepCurve.compounded`` (#15) compounds a modelled path and
    ``MarketSnapshot.compounded`` (#11) a published one. On a flat path they
    have to agree, and both have to be what QuantLib 1.44's
    ``OvernightIndexedCoupon`` pays over SR3 Sep-26, 2026-09-16 to
    2026-12-16, with ``ql.Sofr()`` fixed at the flat rate on every
    UnitedStates(SOFR) business day.
    """

    QUANTLIB = {0.0430: 0.04322943643757335, 0.0533: 0.05365281031184554}

    @pytest.mark.parametrize("rate", sorted(QUANTLIB))
    def test_sr3_sep_26_on_flat_sofr(self, rate):
        from rates_engine.curves.parametric import FOMCStepCurve

        start, end = date(2026, 9, 16), date(2026, 12, 16)
        path = FOMCStepCurve(as_of=AS_OF, meeting_dates=(), segment_rates=(rate,))
        days = [start - timedelta(days=30) + timedelta(days=k) for k in range(130)]
        published = {d: rate for d in days if SIFMA_US.is_business_day(d)}
        history = _fixings(_published("SOFR", published), as_of=date(2026, 12, 31))
        compounded = history.compounded("SOFR", start, end).rate
        assert path.compounded(start, end) == pytest.approx(compounded, rel=1e-14)
        assert compounded == pytest.approx(self.QUANTLIB[rate], abs=1e-15)


class TestAStartRolledBackOverAWeekend:
    """The repro of #10's review: a month-end start that Modified Following rolls before as_of.

    As of Saturday 2026-01-31, a SOFR OIS with effective date that Saturday
    and maturity 2027-02-01 starts, adjusted, on Friday the 30th: before
    the valuation date. #10 alone priced the Friday as if it accrued at a
    discount factor of one (par 401.309 bp), and main by starting on the
    Saturday (402.450 bp). The Friday's SOFR is history and decides the
    whole weekend, since a published rate covers the calendar days to the
    next business day; only from Monday does the curve forecast.
    """

    #: QuantLib 1.44: ``OvernightIndexedSwap`` (annual, ACT/360, payment lag
    #: 2, UnitedStates(SOFR), Modified Following, backward) from 2026-01-31 to
    #: 2027-02-01, schedule 2026-01-30, 2026-02-02, 2027-02-01, on
    #: ``FlatForward(2026-01-31, 4%, Actual365Fixed, Continuous)``, with the
    #: SOFR fixing of 2026-01-30 at 4.00%: ``fairRate()``.
    QUANTLIB_FAIR_BP = 402.4738047068733

    @staticmethod
    def _setting():
        from rates_engine.instruments.swaps import OISSwap

        as_of = date(2026, 1, 31)
        nodes = tuple(as_of + timedelta(days=365 * k) for k in range(1, 11))
        curve = DiscountCurve(as_of, nodes, tuple(math.exp(-0.04 * k) for k in range(1, 11)))
        swap = OISSwap(effective=as_of, maturity=date(2027, 2, 1), fixed_rate=0.04,
                       notional=NOTIONAL)
        return swap, CurveSet(curve), as_of

    def test_without_the_friday_fixing_it_is_refused(self):
        from rates_engine.core.errors import MissingFixingError
        from rates_engine.pricing.linear import par_rate

        swap, curve_set, _ = self._setting()
        with pytest.raises(MissingFixingError, match="2026-01-30"):
            par_rate(swap, curve_set)

    def test_with_it_the_par_rate_is_quantlib_s(self):
        from rates_engine.pricing.linear import par_rate

        swap, curve_set, as_of = self._setting()
        snapshot = _fixings(_published("SOFR", {date(2026, 1, 30): 0.04}), as_of=as_of)
        par = par_rate(swap, curve_set, fixings=snapshot).value * 1e4
        assert par == pytest.approx(self.QUANTLIB_FAIR_BP, abs=1e-9)
        # The Friday's rate is the stub period's whole rate: the weekend is
        # history, not forecast.
        flows = pv(swap, curve_set, fixings=snapshot).cashflows or ()
        stub = next(flow for flow in flows if flow.leg == "float")
        # QuantLib's coupon rate for the stub is 0.039999999999995595 too.
        assert stub.rate == pytest.approx(0.04, abs=1e-14)
        assert stub.fixing is not None and stub.fixing.observed_to == date(2026, 2, 2)


class TestDescribeNamesEveryDollarCalendar:
    """``describe`` listed only SIFMA after #13 added the Federal Reserve's for USD/MXN settlement."""

    def test_both_us_calendars_are_listed(self):
        from rates_engine.app import commands

        payload = commands.COMMANDS["describe"](None)
        assert payload["calendars"] == ["SIFMA_US", "FEDERAL_RESERVE"]
        assert payload["calendars_v3"] == ["BMV"]
