"""PRD-001 AC-6.1 to PRD-001 AC-6.5: par prices at zero, signs mirror, and two curves stay apart."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rates_engine.conventions import DayCount
from rates_engine.curves import CurveSet, DiscountCurve
from rates_engine.instruments import FRA, IRSwap, OISSwap, Side
from rates_engine.pricing import annuity, dv01, par_rate, pv


def _flat(as_of: date, rate: float, years: int = 6) -> DiscountCurve:
    nodes = tuple(as_of + timedelta(days=365 * k) for k in range(1, years + 1))
    return DiscountCurve(as_of, nodes, tuple(math.exp(-rate * k) for k in range(1, years + 1)))


class TestParPricesAtZero:
    """PRD-001 AC-6.1: a swap struck at its own par rate is worth nothing."""

    def test_par_swap_prices_at_zero(self, par_swap, curve_set):
        assert abs(pv(par_swap, curve_set).value) < 1e-8 * par_swap.notional

    def test_it_holds_for_the_receiver_too(self, par_swap, curve_set):
        receiver = replace(par_swap, side=Side.RECEIVER)
        assert abs(pv(receiver, curve_set).value) < 1e-8 * receiver.notional

    def test_off_market_swaps_are_worth_something(self, par_swap, curve_set):
        rich = replace(par_swap, fixed_rate=par_swap.fixed_rate + 0.01)
        assert pv(rich, curve_set).value < -1e-8 * rich.notional

    def test_par_rate_equals_float_pv_over_annuity(self, par_swap, curve_set):
        payer = replace(par_swap, side=Side.PAYER)
        float_pv = sum(
            flow.amount * curve_set.discount.df(flow.payment_date)
            for flow in payer.float_cashflows(curve_set)
        )
        expected = float_pv / (par_swap.notional * annuity(par_swap, curve_set).value)
        assert par_rate(par_swap, curve_set).value == pytest.approx(expected, rel=1e-15)

    def test_annuity_is_positive_and_in_years(self, par_swap, curve_set):
        result = annuity(par_swap, curve_set)
        assert result.unit == "years"
        assert 1.5 < result.value < 2.5


class TestDV01Signs:
    """PRD-001 AC-6.2: payer and receiver mirror each other exactly."""

    def test_signs_are_opposite_and_magnitudes_equal(self, par_swap, curve_set):
        payer = dv01(replace(par_swap, side=Side.PAYER), curve_set).value
        receiver = dv01(replace(par_swap, side=Side.RECEIVER), curve_set).value
        assert payer < 0 < receiver
        assert abs(payer + receiver) < 1e-10

    def test_the_unit_is_declared(self, par_swap, curve_set):
        assert dv01(par_swap, curve_set).unit == "USD_per_bp"

    def test_a_larger_bump_gives_nearly_the_same_dv01(self, par_swap, curve_set):
        # Central differencing, so the bump size is second-order.
        small = dv01(par_swap, curve_set, bump_bp=1.0).value
        large = dv01(par_swap, curve_set, bump_bp=5.0).value
        assert large == pytest.approx(small, rel=1e-5)

    def test_a_non_positive_bump_refuses(self, par_swap, curve_set):
        with pytest.raises(ValueError, match="bump_bp"):
            dv01(par_swap, curve_set, bump_bp=0.0)

    def test_dv01_scales_with_notional(self, par_swap, curve_set):
        doubled = replace(par_swap, notional=par_swap.notional * 2)
        assert dv01(doubled, curve_set).value == pytest.approx(
            2 * dv01(par_swap, curve_set).value, rel=1e-12
        )

    def test_the_bump_basis_is_recorded(self, par_swap, curve_set):
        fields = dv01(par_swap, curve_set).evidence.fields
        assert fields["bump_basis"] == "zero_curve_parallel"
        assert fields["bump_shape"] == "parallel"
        assert fields["difference"] == "central"


class TestMultiCurve:
    """PRD-001 AC-6.3: the tenor curve projects, the OIS curve discounts, and collapsing them agrees."""

    def test_with_no_basis_the_irs_matches_the_ois(self, as_of):
        curve = _flat(as_of, 0.04)
        single = CurveSet(curve)
        ois = OISSwap(
            effective=as_of,
            maturity=as_of + timedelta(days=365 * 4),
            fixed_rate=0.04,
            notional=10_000_000.0,
            payment_lag_days=0,
        )
        irs = IRSwap(
            effective=as_of,
            maturity=as_of + timedelta(days=365 * 4),
            fixed_rate=0.04,
            notional=10_000_000.0,
            fixed_frequency_months=12,
            float_frequency_months=12,
        )
        assert par_rate(irs, single).value == pytest.approx(
            par_rate(ois, single).value, rel=1e-6
        )

    def test_a_basis_moves_the_irs_and_not_the_ois(self, as_of):
        discount = _flat(as_of, 0.04)
        projection = _flat(as_of, 0.045)
        dual = CurveSet(discount, projection)
        single = CurveSet(discount)
        irs = IRSwap(
            effective=as_of,
            maturity=as_of + timedelta(days=365 * 4),
            fixed_rate=0.04,
            notional=10_000_000.0,
        )
        ois = OISSwap(
            effective=as_of,
            maturity=as_of + timedelta(days=365 * 4),
            fixed_rate=0.04,
            notional=10_000_000.0,
            payment_lag_days=0,
        )
        assert par_rate(irs, dual).value > par_rate(irs, single).value + 1e-4
        assert pv(ois, dual).value == pytest.approx(pv(ois, single).value, rel=1e-15)

    def test_the_evidence_says_which_curve_projected(self, as_of):
        dual = CurveSet(_flat(as_of, 0.04), _flat(as_of, 0.045))
        irs = IRSwap(
            effective=as_of, maturity=as_of + timedelta(days=730), fixed_rate=0.04
        )
        fields = pv(irs, dual).evidence.fields
        assert fields["dual_curve"] is True
        assert fields["projection_curve"] == "tenor"
        assert fields["discounting"] == "collateral_rate_ois_sofr"

    def test_single_curve_says_so_too(self, par_swap, curve_set):
        fields = pv(par_swap, curve_set).evidence.fields
        assert fields["dual_curve"] is False
        assert fields["projection_curve"] == "discount"


class TestFRA:
    """PRD-001 AC-6.4: the FRA rate follows the projection curve, both branches."""

    def test_with_a_basis_it_differs_from_the_discount_forward(self, as_of):
        discount = _flat(as_of, 0.04)
        projection = _flat(as_of, 0.045)
        start = as_of + timedelta(days=365)
        end = start + timedelta(days=91)
        fra = FRA(start=start, end=end, rate=0.04)
        fair = fra.fair_rate(CurveSet(discount, projection))
        assert fair != pytest.approx(
            discount.forward(start, end, day_count=DayCount.ACT_360), rel=1e-6
        )
        assert fair == pytest.approx(
            projection.forward(start, end, day_count=DayCount.ACT_360), rel=1e-15
        )

    def test_without_a_basis_the_two_branches_agree(self, as_of):
        discount = _flat(as_of, 0.04)
        start = as_of + timedelta(days=365)
        end = start + timedelta(days=91)
        fra = FRA(start=start, end=end, rate=0.04)
        assert fra.fair_rate(CurveSet(discount, discount)) == pytest.approx(
            fra.fair_rate(CurveSet(discount)), rel=1e-15
        )
        assert fra.fair_rate(CurveSet(discount)) == pytest.approx(
            discount.forward(start, end, day_count=DayCount.ACT_360), rel=1e-15
        )

    def test_struck_at_its_fair_rate_it_prices_at_zero(self, as_of):
        curve_set = CurveSet(_flat(as_of, 0.04))
        start = as_of + timedelta(days=365)
        end = start + timedelta(days=91)
        provisional = FRA(start=start, end=end, rate=0.0)
        fra = FRA(start=start, end=end, rate=provisional.fair_rate(curve_set))
        assert abs(pv(fra, curve_set).value) < 1e-9 * fra.notional

    def test_payer_and_receiver_mirror(self, as_of):
        curve_set = CurveSet(_flat(as_of, 0.04))
        start = as_of + timedelta(days=365)
        end = start + timedelta(days=91)
        payer = FRA(start=start, end=end, rate=0.03, side=Side.PAYER)
        receiver = FRA(start=start, end=end, rate=0.03, side=Side.RECEIVER)
        assert pv(payer, curve_set).value == pytest.approx(-pv(receiver, curve_set).value)


class TestSideValidation:
    """A side that is neither payer nor receiver is not guessed at."""

    def test_unknown_side_refuses(self, as_of, curve_set):
        swap = OISSwap(
            effective=as_of, maturity=as_of + timedelta(days=730), fixed_rate=0.04, side="long"
        )
        with pytest.raises(ValueError, match="payer"):
            pv(swap, curve_set)
