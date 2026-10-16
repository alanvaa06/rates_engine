"""PRD-001 AC-4.1 to PRD-001 AC-4.5: four views of one curve, and the conversions between them."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rates_engine.conventions.daycount import CURVE_TIME_BASIS, DayCount, year_fraction
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.curves.discount import DiscountCurve
from rates_engine.curves.views import all_views, par_curve, zero_curve


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
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.pricing.linear import pv

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
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.pricing.linear import par_rate

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

    def test_it_declares_its_start_and_payment_lag(self, flat_curve):
        """The default par view starts on ``as_of`` and pays on the period end.
        A quoted SOFR OIS starts two business days later and pays two business
        days after each period, so the two differ -- by 0.02 to 0.47 bp on the
        review's strip -- and the payload used to give no hint why."""
        curve = flat_curve.curve
        payload = par_curve(curve).to_dict()
        assert payload["start"] == curve.as_of.isoformat()
        assert payload["payment_lag_days"] == 0

    def test_on_the_quoted_conventions_it_reproduces_the_quotes(self):
        """Spot start and a two-day lag, as the swaps were quoted: the par view
        then returns the quotes the curve was built from."""
        from rates_engine.conventions.schedule import add_months
        from rates_engine.curves.bootstrap import bootstrap_discount_curve
        from rates_engine.instruments.swaps import OISSwap
        from rates_engine.pricing.calibration import SwapQuoteNode

        as_of, spot = date(2026, 1, 15), date(2026, 1, 20)
        quotes = ((1, 0.0431), (6, 0.0419), (12, 0.0402), (24, 0.0375), (60, 0.0358))
        nodes = tuple(
            SwapQuoteNode(
                OISSwap(effective=spot, maturity=add_months(spot, months), fixed_rate=q), q
            )
            for months, q in quotes
        )
        curve = bootstrap_discount_curve(as_of, nodes).curve
        maturities = tuple(add_months(spot, months) for months, _ in quotes)
        quoted = par_curve(curve, maturities, start=spot, payment_lag_days=2)
        for value, (_, expected) in zip(quoted.values, quotes, strict=True):
            assert abs(value - expected) * 1e4 < 1e-8
        plain = par_curve(curve, maturities)
        assert max(abs(v - q) for v, (_, q) in zip(plain.values, quotes, strict=True)) > 1e-6

    def test_all_views_passes_the_par_conventions_through(self, flat_curve):
        curve = flat_curve.curve
        start = curve.as_of + timedelta(days=5)
        views = all_views(curve, par_start=start, par_payment_lag_days=2)
        payload = views.to_dict()
        assert payload["par"]["start"] == start.isoformat()
        assert payload["par"]["payment_lag_days"] == 2
        assert views.evidence.fields["par_start"] == start.isoformat()
        assert views.evidence.fields["par_payment_lag_days"] == 2

    def test_the_default_all_views_says_what_its_par_view_is(self, flat_curve):
        views = all_views(flat_curve.curve)
        fields = views.evidence.fields
        assert fields["par_start"] == flat_curve.curve.as_of.isoformat()
        assert fields["par_payment_lag_days"] == 0
        assert "not the quoted" in fields["par_note"]


class TestRoundTrip:
    """PRD-001 AC-4.4: par to zero to par returns the curve it started from."""

    def test_flat_par_curve_survives_the_round_trip(self):
        as_of = date(2026, 1, 15)
        maturities = tuple(date(2026 + k, 1, 15) for k in range(1, 8))
        target = 0.04

        # Bootstrap discount factors from a flat 4% par curve, annual, on the
        # period ends the swaps actually use: anniversaries adjusted Modified
        # Following (2028-01-15 is a Saturday, so that period ends on the 18th).
        from rates_engine.conventions.calendar import SIFMA_US, BusinessDayConvention

        ends = tuple(
            SIFMA_US.adjust(m, BusinessDayConvention.MODIFIED_FOLLOWING) for m in maturities
        )
        dfs: list[float] = []
        for index, end in enumerate(ends):
            annuity = sum(
                year_fraction(as_of if k == 0 else ends[k - 1], ends[k], DayCount.ACT_360)
                * dfs[k]
                for k in range(index)
            )
            tau = year_fraction(as_of if index == 0 else ends[index - 1], end, DayCount.ACT_360)
            dfs.append((1.0 - target * annuity) / (1.0 + target * tau))
        curve = DiscountCurve(as_of, ends, tuple(dfs))

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
