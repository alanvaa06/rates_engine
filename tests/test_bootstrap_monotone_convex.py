"""PRD-002 AC-5.1 on swaps: monotone convex reprices an OIS strip, not only a futures strip.

Hagan-West is not local. The instantaneous forward at node ``i`` blends the
discrete forwards on both sides of it, so solving node ``i + 1`` reshapes the
interval that ends at node ``i``. A futures period or a realised stub reads
discount factors only on nodes, where that reshaping cancels, and repriced
under the old one-pass bootstrap. A SOFR OIS does not: it pays two business
days after each period, from a spot two days after the valuation date, and
both of those discount factors sit inside an interval. On the twelve-swap
strip below the one-pass bootstrap left residuals of +0.093 to +0.1145 bp,
``strict=True`` raised on the first swap, and ``compare_interpolations``
raised with it.

The reference is the quotes themselves, which are inputs, and QuantLib 1.44:
its iteratively bootstrapped ``PiecewiseConvexMonotoneZero`` and
``PiecewiseConvexMonotoneForward`` reprice these same twelve helpers to
6.4e-9 and 5.0e-9 bp. QuantLib's convex monotone interpolates a different
quantity, so its discount factors are not a reference for ours; that it
reprices is the claim being matched.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from rates_engine.conventions.schedule import add_months
from rates_engine.core.errors import BootstrapResidualError, CurveArbitrageError
from rates_engine.curves.bootstrap import (
    DEFAULT_ITERATION_TOLERANCE,
    DEFAULT_MAX_ITERATIONS,
    bootstrap_discount_curve,
)
from rates_engine.curves.comparison import compare_interpolations
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.swaps import OISSwap
from rates_engine.pricing.calibration import SwapQuoteNode
from rates_engine.pricing.linear import par_rate

AS_OF = date(2026, 1, 15)
SPOT = date(2026, 1, 20)  # T+2 on SIFMA: 19 January 2026 is Martin Luther King Day

OIS_QUOTES = (
    ("1M", 1, 0.0431),
    ("3M", 3, 0.0428),
    ("6M", 6, 0.0419),
    ("1Y", 12, 0.0402),
    ("2Y", 24, 0.0375),
    ("3Y", 36, 0.0362),
    ("5Y", 60, 0.0358),
    ("7Y", 84, 0.0363),
    ("10Y", 120, 0.0372),
    ("15Y", 180, 0.0384),
    ("20Y", 240, 0.0389),
    ("30Y", 360, 0.0382),
)
"""A SOFR OIS strip that is not flat, annual ACT/360 with a two-day payment lag."""

QUANTLIB_LOG_LINEAR = (
    (date(2026, 2, 24), 0.9952313820327061),
    (date(2026, 4, 22), 0.988589998058649),
    (date(2026, 7, 22), 0.9785639917628224),
    (date(2027, 1, 22), 0.9600647317414336),
    (date(2028, 1, 24), 0.9272727541924561),
    (date(2029, 1, 24), 0.8967580335990464),
    (date(2031, 1, 23), 0.836184257965648),
    (date(2033, 1, 24), 0.7755456468089436),
    (date(2036, 1, 24), 0.6888724858439128),
    (date(2041, 1, 24), 0.5600816215924328),
    (date(2046, 1, 24), 0.45645791030566524),
    (date(2056, 1, 24), 0.319988037636447),
)
"""QuantLib 1.44 ``PiecewiseLogLinearDiscount`` on ``OISRateHelper(2, tenor, quote,
Sofr(), paymentLag=2, paymentFrequency=Annual)`` for the same twelve quotes,
ACT/365F, evaluation date 2026-01-15: pillar dates and discount factors, printed
with ``repr`` in a scratch environment and pasted here."""


def _nodes() -> tuple[SwapQuoteNode, ...]:
    return tuple(
        SwapQuoteNode(
            swap=OISSwap(effective=SPOT, maturity=add_months(SPOT, months), fixed_rate=quote),
            quoted_rate=quote,
            label=label,
        )
        for label, months, quote in OIS_QUOTES
    )


@pytest.fixture(scope="module")
def nodes() -> tuple[SwapQuoteNode, ...]:
    """The twelve quotes as calibration nodes on real swaps."""
    return _nodes()


@pytest.fixture(scope="module")
def convex(nodes):
    """The strip bootstrapped under monotone convex, strictly."""
    return bootstrap_discount_curve(AS_OF, nodes, interpolation="monotone_convex")


class TestMonotoneConvexRepricesSwaps:
    """The strip that broke the one-pass bootstrap now reprices, under strict."""

    def test_strict_succeeds(self, convex):
        assert convex.dropped_instruments == ()

    def test_every_residual_is_at_solver_noise(self, convex):
        # QuantLib reaches 6.4e-9 bp on the same helpers; the one-pass
        # bootstrap left 0.1145 bp.
        worst = max(abs(r) for r in convex.residuals_bp.values())
        assert worst < 1e-8, f"worst residual {worst:.3e} bp"

    def test_the_pricer_agrees_with_every_quote(self, convex, nodes):
        """Through ``par_rate``, not the node's own residual, so the claim is
        about the curve a caller receives."""
        curve_set = CurveSet(convex.curve)
        for node in nodes:
            implied = par_rate(node.swap, curve_set).value
            assert abs(implied - node.quoted_rate) * 1e4 < 1e-8, node.label

    def test_the_curve_is_still_monotone(self, convex):
        assert convex.curve.rising_segments == ()

    def test_the_sweeps_are_recorded(self, convex):
        fields = convex.evidence.fields
        assert fields["iterations"] >= 2
        assert fields["iterations"] <= fields["max_iterations"] == DEFAULT_MAX_ITERATIONS
        assert fields["iteration_tolerance"] == DEFAULT_ITERATION_TOLERANCE

    def test_too_few_sweeps_refuses_by_name(self, nodes):
        with pytest.raises(BootstrapResidualError, match="did not converge"):
            bootstrap_discount_curve(
                AS_OF, nodes, interpolation="monotone_convex", max_iterations=1
            )

    def test_the_refusal_holds_under_lenient_mode_too(self, nodes):
        # strict=False records instruments outside tolerance; a curve that is
        # not a fixed point is not one of those, it is a solver that stopped.
        with pytest.raises(BootstrapResidualError, match="did not converge"):
            bootstrap_discount_curve(
                AS_OF, nodes, interpolation="monotone_convex", max_iterations=1, strict=False
            )

    def test_the_iteration_settings_must_make_sense(self, nodes):
        with pytest.raises(ValueError, match="max_iterations"):
            bootstrap_discount_curve(AS_OF, nodes, max_iterations=0)
        with pytest.raises(ValueError, match="iteration_tolerance"):
            bootstrap_discount_curve(AS_OF, nodes, iteration_tolerance=0.0)

    def test_compare_interpolations_runs_on_swaps(self, nodes):
        comparison = compare_interpolations(AS_OF, nodes)
        assert comparison.max_residual_bp < 1e-8
        assert comparison.max_forward_difference_bp > 0.0

    def test_the_two_curves_differ_at_the_nodes_on_swaps(self, nodes):
        """Unlike a futures strip: an instrument that reads a factor inside an
        interval makes the node factors depend on the interpolation."""
        comparison = compare_interpolations(AS_OF, nodes)
        gaps = [
            abs(comparison.monotone_convex.df(node) - comparison.log_linear.df(node))
            for node in comparison.log_linear.nodes
        ]
        assert max(gaps) > 1e-9


class TestTheFirstPassIsOnlyASeed:
    """Alan Vazquez's PR #15 review: a valid monotone convex curve the first pass refused.

    SOFR OIS at 1Y 3.00%, 2Y 1.502% and 3Y 2.00%. Solved before the 3Y node
    exists, the 2Y quote needs a factor above the 1Y one, and the first pass
    refused it with ``CurveArbitrageError``. Once the 3Y node is in place the
    sweeps settle on a monotone curve: forwards of 0.38 bp over 2027-28 and
    about 301 bp (ACT/360; 305.15 bp on ACT/365) over 2028-29.
    """

    @pytest.fixture
    def quotes(self) -> tuple[SwapQuoteNode, ...]:
        return tuple(
            SwapQuoteNode(
                OISSwap(effective=SPOT, maturity=add_months(SPOT, 12 * years), fixed_rate=rate),
                rate,
                label=f"{years}Y",
            )
            for years, rate in ((1, 0.03), (2, 0.01502), (3, 0.02))
        )

    def test_monotone_convex_fits_it(self, quotes):
        result = bootstrap_discount_curve(AS_OF, quotes, interpolation="monotone_convex")
        assert max(abs(r) for r in result.residuals_bp.values()) < 1e-8
        assert result.curve.rising_segments == ()
        nodes = (AS_OF, *result.curve.nodes)
        forwards = [
            result.curve.forward(a, b, compounding="continuous") * 1e4
            for a, b in zip(nodes, nodes[1:], strict=False)
        ]
        assert forwards[1] == pytest.approx(0.38, abs=0.01)
        assert forwards[1] > 0.0

    def test_log_linear_still_refuses_it(self, quotes):
        """Log-linear is local, its first pass is final, and on it the 2Y quote
        really does need a rising factor."""
        with pytest.raises(CurveArbitrageError, match="2Y"):
            bootstrap_discount_curve(AS_OF, quotes)

    def test_a_curve_that_stays_rising_is_still_refused(self):
        """The seed may rise; the settled curve may not. A 2Y quote far below
        the 1Y one needs a negative forward under any interpolation."""
        quotes = tuple(
            SwapQuoteNode(
                OISSwap(effective=SPOT, maturity=add_months(SPOT, 12 * years), fixed_rate=rate),
                rate,
                label=f"{years}Y",
            )
            for years, rate in ((1, 0.03), (2, 0.005), (3, 0.02))
        )
        with pytest.raises(CurveArbitrageError, match="rises"):
            bootstrap_discount_curve(AS_OF, quotes, interpolation="monotone_convex")


class TestLogLinearIsUntouched:
    """Log-linear is local, so its one pass is the fixed point and no sweep runs."""

    def test_it_matches_quantlib_at_every_pillar(self, nodes):
        result = bootstrap_discount_curve(AS_OF, nodes)
        assert result.curve.nodes == tuple(d for d, _ in QUANTLIB_LOG_LINEAR)
        for node, (_, expected) in zip(result.curve.nodes, QUANTLIB_LOG_LINEAR, strict=True):
            years = (node - AS_OF).days / 365.0
            gap_bp = abs(math.log(result.curve.df(node)) - math.log(expected)) / years * 1e4
            assert gap_bp < 1e-8, f"{node}: zero rate off QuantLib by {gap_bp:.3e} bp"

    def test_no_sweep_is_run(self, nodes):
        assert bootstrap_discount_curve(AS_OF, nodes).evidence.fields["iterations"] == 0

    def test_it_reprices_to_noise(self, nodes):
        result = bootstrap_discount_curve(AS_OF, nodes)
        assert max(abs(r) for r in result.residuals_bp.values()) < 1e-9
