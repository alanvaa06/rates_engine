"""Property tests: the invariants that must hold for curves nobody wrote by hand.

The example-based tests elsewhere check a flat 4% curve because it makes the
analytics checkable. These check shapes nobody chose — upward, downward and
humped, at levels from near zero to ten percent — because that is where a
convention bug that the flat case hides will show up.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from rates_engine.conventions import DayCount, imm_date, next_imm_on_or_after, year_fraction
from rates_engine.curves import (
    CurveSet,
    DiscountCurve,
    FuturesNode,
    RealizedStubNode,
    bootstrap_discount_curve,
    par_curve,
)
from rates_engine.curves.discount import CURVE_TIME_BASIS
from rates_engine.instruments import OISSwap, Side
from rates_engine.pricing import dv01, par_rate, pv
from rates_engine.risk import key_rate_dv01, money_convexity, tent_weights

AS_OF = date(2026, 1, 15)
SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

forwards = st.lists(
    st.floats(min_value=0.0001, max_value=0.10, allow_nan=False, allow_infinity=False),
    min_size=4,
    max_size=9,
)


def _curve_from(rates: list[float]) -> tuple[DiscountCurve, tuple, date, date]:
    start = imm_date(2026, 3)
    instruments: list[object] = [
        RealizedStubNode(
            end=start, accrual_factor=1.0 + rates[0] * ((start - AS_OF).days / 360.0)
        )
    ]
    period = start
    for index, rate in enumerate(rates):
        following = next_imm_on_or_after(period + timedelta(days=1))
        instruments.append(
            FuturesNode(start=period, end=following, forward_rate=rate, label=f"f{index}")
        )
        period = following
    result = bootstrap_discount_curve(AS_OF, tuple(instruments))
    return result.curve, tuple(instruments), start, period


class TestCurveInvariants:
    """Whatever the forwards, the curve that comes out is a curve."""

    @given(rates=forwards)
    @SETTINGS
    def test_discount_factors_are_positive_and_non_increasing(self, rates):
        curve, _, _, _ = _curve_from(rates)
        assert all(df > 0 for df in curve.dfs)
        assert all(b <= a for a, b in zip(curve.dfs, curve.dfs[1:], strict=False))

    @given(rates=forwards)
    @SETTINGS
    def test_every_input_forward_comes_back(self, rates):
        curve, instruments, _, _ = _curve_from(rates)
        for instrument in instruments:
            if isinstance(instrument, FuturesNode):
                assert curve.forward(
                    instrument.start, instrument.end, day_count=DayCount.ACT_360
                ) == pytest.approx(instrument.forward_rate, abs=1e-11)

    @given(rates=forwards)
    @SETTINGS
    def test_bootstrap_is_idempotent(self, rates):
        first, instruments, _, _ = _curve_from(rates)
        second = bootstrap_discount_curve(AS_OF, instruments).curve
        assert first.dfs == second.dfs

    @given(rates=forwards)
    @SETTINGS
    def test_the_zero_curve_reproduces_the_discount_factors(self, rates):
        curve, _, _, _ = _curve_from(rates)
        for node in curve.nodes:
            tau = year_fraction(AS_OF, node, CURVE_TIME_BASIS)
            assert math.exp(-curve.zero(node) * tau) == pytest.approx(curve.df(node), rel=1e-13)

    @given(rates=forwards)
    @SETTINGS
    def test_forwards_are_non_negative_when_the_inputs_are(self, rates):
        curve, _, _, _ = _curve_from(rates)
        for first, second in zip(curve.nodes, curve.nodes[1:], strict=False):
            assert curve.forward(first, second) >= -1e-12


class TestPricingInvariants:
    """A swap at par is worth nothing, and the two sides mirror."""

    @given(rates=forwards)
    @SETTINGS
    def test_a_swap_at_its_par_rate_prices_at_zero(self, rates):
        curve, _, start, end = _curve_from(rates)
        curve_set = CurveSet(curve)
        provisional = OISSwap(
            effective=start, maturity=end, fixed_rate=0.04, notional=10_000_000.0
        )
        at_par = replace(provisional, fixed_rate=par_rate(provisional, curve_set).value)
        assert abs(pv(at_par, curve_set).value) < 1e-8 * at_par.notional

    @given(rates=forwards)
    @SETTINGS
    def test_payer_and_receiver_dv01_mirror(self, rates):
        curve, _, start, end = _curve_from(rates)
        curve_set = CurveSet(curve)
        payer = OISSwap(effective=start, maturity=end, fixed_rate=0.04, side=Side.PAYER)
        receiver = replace(payer, side=Side.RECEIVER)
        assert dv01(payer, curve_set).value == pytest.approx(
            -dv01(receiver, curve_set).value, rel=1e-10
        )

    @given(rates=forwards)
    @SETTINGS
    def test_pricing_is_idempotent(self, rates):
        curve, _, start, end = _curve_from(rates)
        curve_set = CurveSet(curve)
        swap = OISSwap(effective=start, maturity=end, fixed_rate=0.04)
        assert pv(swap, curve_set).value == pv(swap, curve_set).value

    @given(rates=forwards)
    @SETTINGS
    def test_a_receiver_has_positive_dv01(self, rates):
        curve, _, start, end = _curve_from(rates)
        receiver = OISSwap(
            effective=start, maturity=end, fixed_rate=0.04, side=Side.RECEIVER
        )
        assert dv01(receiver, CurveSet(curve)).value > 0

    @given(rates=forwards)
    @SETTINGS
    def test_the_par_curve_round_trips_through_pricing(self, rates):
        curve, _, _, end = _curve_from(rates)
        curve_set = CurveSet(curve)
        view = par_curve(curve, (end,), frequency_months=12, payment_lag_days=0)
        swap = OISSwap(
            effective=AS_OF,
            maturity=end,
            fixed_rate=view.values[0],
            notional=10_000_000.0,
            payment_lag_days=0,
        )
        assert abs(pv(swap, curve_set).value) < 1e-7 * swap.notional


class TestRiskInvariants:
    """The key-rate identity, on curves nobody designed for it."""

    @given(
        rates=forwards,
        count=st.integers(min_value=2, max_value=5),
    )
    @SETTINGS
    def test_key_rates_sum_to_the_parallel_dv01(self, rates, count):
        curve, _, start, end = _curve_from(rates)
        curve_set = CurveSet(curve)
        span = year_fraction(AS_OF, curve.nodes[-1], CURVE_TIME_BASIS)
        tenors = tuple(span * (index + 1) / count for index in range(count))
        assume(all(b > a for a, b in zip(tenors, tenors[1:], strict=False)))
        swap = OISSwap(
            effective=start, maturity=end, fixed_rate=0.04, notional=10_000_000.0
        )
        profile = key_rate_dv01(swap, curve_set, tenors)
        parallel = dv01(swap, curve_set).value
        assert profile.total == pytest.approx(parallel, rel=1e-6)

    @given(
        time=st.floats(min_value=0.0, max_value=40.0, allow_nan=False),
        tenors=st.lists(
            st.floats(min_value=0.1, max_value=30.0, allow_nan=False),
            min_size=1,
            max_size=8,
            unique=True,
        ),
    )
    @settings(max_examples=200, deadline=None)
    def test_tent_weights_always_partition_unity(self, time, tenors):
        ordered = tuple(sorted(tenors))
        assume(all(b - a > 1e-6 for a, b in zip(ordered, ordered[1:], strict=False)))
        weights = tent_weights(time, ordered)
        assert sum(weights) == pytest.approx(1.0, abs=1e-12)
        assert all(weight >= 0.0 for weight in weights)

    @given(rates=forwards)
    @SETTINGS
    def test_payer_and_receiver_convexity_mirror(self, rates):
        curve, _, start, end = _curve_from(rates)
        curve_set = CurveSet(curve)
        payer = OISSwap(effective=start, maturity=end, fixed_rate=0.04, side=Side.PAYER)
        receiver = replace(payer, side=Side.RECEIVER)
        assert money_convexity(payer, curve_set).value == pytest.approx(
            -money_convexity(receiver, curve_set).value, rel=1e-8
        )
