"""PRD-001 AC-4.1 to PRD-001 AC-4.5: four views of one curve, and the conversions between them."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rates_engine.conventions import DayCount, year_fraction
from rates_engine.curves import (
    DiscountCurve,
    all_views,
    par_curve,
    zero_curve,
)
from rates_engine.curves.discount import CURVE_TIME_BASIS
from rates_engine.errors import UnsupportedConventionError


class TestZeroCurve:
    """PRD-001 AC-4.1: the three compounding bases agree with each other."""

    def test_continuous_is_minus_log_df_over_t(self, flat_curve):
        curve = flat_curve.curve
        for node in curve.nodes:
            tau = year_fraction(curve.as_of, node, CURVE_TIME_BASIS)
            assert curve.zero(node) == pytest.approx(-math.log(curve.df(node)) / tau, rel=1e-15)

    def test_annual_is_consistent_with_continuous(self, flat_curve):
        curve = flat_curve.curve
        for node in curve.nodes:
            tau = year_fraction(curve.as_of, node, CURVE_TIME_BASIS)
            continuous = curve.zero(node, compounding="continuous")
            annual = curve.zero(node, compounding="annual")
            assert (1.0 + annual) ** tau == pytest.approx(math.exp(continuous * tau), rel=1e-12)

    def test_simple_is_consistent_with_continuous(self, flat_curve):
        curve = flat_curve.curve
        for node in curve.nodes:
            tau = year_fraction(curve.as_of, node, CURVE_TIME_BASIS)
            simple = curve.zero(node, compounding="simple")
            continuous = curve.zero(node, compounding="continuous")
            assert 1.0 + simple * tau == pytest.approx(math.exp(continuous * tau), rel=1e-12)

    def test_unknown_compounding_refuses(self, flat_curve):
        with pytest.raises(UnsupportedConventionError, match="quarterly"):
            flat_curve.curve.zero(flat_curve.curve.nodes[0], compounding="quarterly")

    def test_a_maturity_at_the_valuation_date_has_no_zero_rate(self, flat_curve):
        with pytest.raises(ValueError, match="after"):
            flat_curve.curve.zero(flat_curve.curve.as_of)


class TestForwardRelation:
    """PRD-001 AC-4.2: the annual-compounded no-arbitrage identity, to 1e-12."""

    def test_spot_forward_identity_holds(self, flat_curve):
        curve = flat_curve.curve
        nodes = curve.nodes
        for first, second in zip(nodes, nodes[1:], strict=False):
            t1 = year_fraction(curve.as_of, first, CURVE_TIME_BASIS)
            t2 = year_fraction(curve.as_of, second, CURVE_TIME_BASIS)
            s1 = curve.zero(first, compounding="annual")
            s2 = curve.zero(second, compounding="annual")
            forward = curve.forward(
                first, second, day_count=CURVE_TIME_BASIS, compounding="annual"
            )
            left = (1.0 + s2) ** t2
            right = (1.0 + s1) ** t1 * (1.0 + forward) ** (t2 - t1)
            assert left == pytest.approx(right, rel=1e-12)

    def test_simple_forward_is_the_discount_factor_ratio(self, flat_curve):
        curve = flat_curve.curve
        first, second = curve.nodes[0], curve.nodes[1]
        tau = year_fraction(first, second, DayCount.ACT_360)
        expected = (curve.df(first) / curve.df(second) - 1.0) / tau
        assert curve.forward(first, second) == pytest.approx(expected, rel=1e-15)

    def test_a_zero_length_forward_refuses(self, flat_curve):
        node = flat_curve.curve.nodes[0]
        with pytest.raises(ValueError, match="positive"):
            flat_curve.curve.forward(node, node)

    def test_unknown_forward_compounding_refuses(self, flat_curve):
        curve = flat_curve.curve
        with pytest.raises(UnsupportedConventionError):
            curve.forward(curve.nodes[0], curve.nodes[1], compounding="monthly")


class TestParCurve:
    """PRD-001 AC-4.3: the par rate is the rate that prices a swap at zero."""

    def test_par_rate_prices_its_own_swap_at_zero(self, flat_curve, curve_set):
        from rates_engine.instruments import OISSwap
        from rates_engine.pricing import pv

        curve = flat_curve.curve
        view = par_curve(curve, (curve.nodes[-1],), frequency_months=12, payment_lag_days=0)
        swap = OISSwap(
            effective=curve.as_of,
            maturity=curve.nodes[-1],
            fixed_rate=view.values[0],
            notional=10_000_000.0,
            payment_lag_days=0,
        )
        assert abs(pv(swap, curve_set).value) < 1e-8 * swap.notional

    def test_it_agrees_with_pricing_par_rate_on_the_same_schedule(self, flat_curve, curve_set):
        # The two live in different modules and must not drift: one schedule
        # generator, one answer.
        from rates_engine.instruments import OISSwap
        from rates_engine.pricing import par_rate

        curve = flat_curve.curve
        view = par_curve(curve, (curve.nodes[-1],), frequency_months=12, payment_lag_days=2)
        swap = OISSwap(
            effective=curve.as_of,
            maturity=curve.nodes[-1],
            fixed_rate=0.04,
            notional=1_000_000.0,
            payment_lag_days=2,
        )
        assert view.values[0] == pytest.approx(par_rate(swap, curve_set).value, rel=1e-14)

    def test_a_maturity_before_the_start_refuses(self, flat_curve):
        curve = flat_curve.curve
        with pytest.raises(ValueError, match="after"):
            par_curve(curve, (curve.as_of,))


class TestRoundTrip:
    """PRD-001 AC-4.4: par to zero to par returns the curve it started from."""

    def test_flat_par_curve_survives_the_round_trip(self):
        as_of = date(2026, 1, 15)
        maturities = tuple(date(2026 + k, 1, 15) for k in range(1, 8))
        target = 0.04

        # Bootstrap discount factors from a flat 4% par curve, annual.
        dfs: list[float] = []
        for index, maturity in enumerate(maturities):
            annuity = sum(
                year_fraction(
                    as_of if k == 0 else maturities[k - 1], maturities[k], DayCount.ACT_360
                )
                * dfs[k]
                for k in range(index)
            )
            tau = year_fraction(
                as_of if index == 0 else maturities[index - 1], maturity, DayCount.ACT_360
            )
            dfs.append((1.0 - target * annuity) / (1.0 + target * tau))
        curve = DiscountCurve(as_of, maturities, tuple(dfs))

        recovered = par_curve(curve, maturities, frequency_months=12)
        for value in recovered.values:
            assert value == pytest.approx(target, abs=1e-8)

    def test_zero_curve_reproduces_the_discount_factors(self, flat_curve):
        curve = flat_curve.curve
        view = zero_curve(curve)
        for node, rate in zip(view.dates, view.values, strict=True):
            tau = year_fraction(curve.as_of, node, CURVE_TIME_BASIS)
            assert math.exp(-rate * tau) == pytest.approx(curve.df(node), rel=1e-14)


class TestConventionsTravel:
    """PRD-001 AC-4.5: no rate is exported without the convention that defines it."""

    def test_each_view_declares_its_conventions(self, flat_curve):
        views = all_views(flat_curve.curve, source_evidence=flat_curve.evidence)
        payload = views.to_dict()
        assert payload["zero"]["compounding"] == "continuous"
        assert payload["zero"]["day_count"] == "ACT/365F"
        assert payload["par"]["frequency_months"] == 12
        assert payload["par"]["day_count"] == "ACT/360"
        assert payload["forward"]["tenor_months"] == 3
        assert payload["forward"]["compounding"] == "simple"

    def test_discount_factors_declare_no_compounding_rather_than_a_wrong_one(self, flat_curve):
        payload = all_views(flat_curve.curve).to_dict()
        assert payload["discount"]["compounding"] is None
        assert payload["discount"]["day_count"] is None

    def test_every_view_reports_its_dates(self, flat_curve):
        payload = all_views(flat_curve.curve).to_dict()
        for kind in ("discount", "zero", "par", "forward"):
            assert len(payload[kind]["dates"]) == len(payload[kind]["values"])
            assert payload[kind]["kind"] == kind

    def test_the_curve_evidence_chains_through(self, flat_curve):
        views = all_views(flat_curve.curve, source_evidence=flat_curve.evidence)
        assert views.evidence.sources[0].produced_by == "curves.bootstrap_discount_curve"

    def test_a_non_default_compounding_is_reported_as_chosen(self, flat_curve):
        views = all_views(flat_curve.curve, compounding="annual")
        assert views.to_dict()["zero"]["compounding"] == "annual"


class TestExtrapolation:
    """Past the last node the forward is held flat, and it is continuous there."""

    def test_forwards_are_continuous_at_the_last_node(self, flat_curve):
        curve = flat_curve.curve
        last = curve.nodes[-1]
        inside = curve.forward(last - timedelta(days=30), last)
        outside = curve.forward(last, last + timedelta(days=30))
        assert outside == pytest.approx(inside, rel=1e-3)

    def test_discount_factors_stay_monotone_past_the_end(self, flat_curve):
        curve = flat_curve.curve
        beyond = curve.nodes[-1] + timedelta(days=400)
        assert curve.df(beyond) < curve.df(curve.nodes[-1])

    def test_before_the_first_node_it_interpolates_from_one(self, flat_curve):
        curve = flat_curve.curve
        early = curve.as_of + timedelta(days=5)
        assert curve.df(curve.as_of) == 1.0
        assert 1.0 > curve.df(early) > curve.df(curve.nodes[0])
