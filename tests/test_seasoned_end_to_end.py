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

    Before #20 the linear pricer kept a private constant for Term SOFR and
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
