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
        assert measured == pytest.approx(-17_203.37, abs=0.01)
        assert dv01(irs, curve_set).value == pytest.approx(-19_666.17, abs=0.01)

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

    def test_a_forecast_standing_in_for_the_fixing_of_as_of_degrades_the_dv01(
        self, strip_curve
    ):
        from rates_engine.instruments.swaps import IRSwap
        from rates_engine.risk.sensitivities import dv01

        irs = IRSwap(effective=_SPOT, maturity=date(2028, 1, 20), fixed_rate=0.039,
                     notional=NOTIONAL)
        forecast = dv01(irs, CurveSet(strip_curve))
        assert forecast.evidence.worst_quality is DataQuality.PROXY
        snapshot = _fixings(_published("TERM_SOFR_3M", {AS_OF: 0.0405}))
        read = dv01(irs, CurveSet(strip_curve), fixings=snapshot)
        assert read.evidence.worst_quality is DataQuality.OBSERVED


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
            strip_hedge(swap, nodes, as_of=AS_OF, require_full_coverage=False)
        hedge = strip_hedge(swap, nodes, as_of=AS_OF, require_full_coverage=False,
                            fixings=snapshot)
        assert hedge.swap_dv01 == dv01(swap, hedge.curve_set, fixings=snapshot).value
        assert hedge.fixings is snapshot
        table = shock_table(hedge, (-25.0, 25.0))
        assert len(table.table) == 2
        assert "SOFR" in {p.series_id for p in _inputs(table.evidence)}

    def test_the_coverage_a_seasoned_swap_needs_starts_at_as_of(self):
        # Nothing before as_of has rate risk left. The refusal names as_of,
        # not the trade date of a swap struck six months ago.
        from rates_engine.core.errors import IncompleteStripError
        from rates_engine.hedging.futures_strip import strip_hedge
        from rates_engine.instruments.swaps import OISSwap

        nodes, start, end = self._strip()
        swap = OISSwap(effective=date(2025, 7, 15), maturity=end, fixed_rate=0.04,
                       notional=NOTIONAL)
        with pytest.raises(IncompleteStripError, match="2026-01-15") as caught:
            strip_hedge(swap, nodes, as_of=AS_OF, fixings=_fixings(_sofr_history()))
        assert "2025-07-15" not in str(caught.value)


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
        "hedge": {"require_full_coverage": False, "shocks_bp": [-25, 25]},
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
        from rates_engine.core.errors import ConfigurationError

        config = _seasoned_config()
        config["fixings"]["SOFR"]["values"][3]["rate"] = 4.30
        with pytest.raises(ConfigurationError, match=r"fixings\.SOFR\.values\[3\]\.rate"):
            commands.COMMANDS["price"](config)

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
