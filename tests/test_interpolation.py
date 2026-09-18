"""PRD-002 AC-5.1 and PRD-002 AC-5.2: two interpolations, one calibration, and positivity.

The claim being tested has two halves that pull in opposite directions.
Both interpolations reproduce the discrete forwards between nodes exactly,
so both reprice every calibration instrument — changing the interpolation
does not change the fit. And they disagree about the instantaneous forward
*between* nodes, which no instrument constrains, so changing the
interpolation does change anything that reads a forward off the curve.
Asserting the first while measuring the second is the honest way to show
what the choice buys.

PRD-002 AC-5.2 is Hagan and West's positivity result. The tests below pin it to the
mechanism — the step-2 collar — rather than just observing that it holds on
one curve, by turning the collar off and showing what breaks.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from rates_engine.curves.bootstrap import FuturesNode, RealizedStubNode
from rates_engine.curves.comparison import compare_interpolations
from rates_engine.curves.discount import INTERPOLATIONS, DiscountCurve
from rates_engine.curves.interpolation import (
    MonotoneConvex,
    discrete_forwards,
    node_forwards,
)
from rates_engine.errors import UnsupportedConventionError

AS_OF = date(2026, 9, 16)


def _log_dfs(times: tuple[float, ...], forwards: tuple[float, ...]) -> tuple[float, ...]:
    """Log discount factors whose interval forwards are exactly ``forwards``."""
    total, out, previous = 0.0, [], 0.0
    for time, forward in zip(times, forwards, strict=True):
        total -= forward * (time - previous)
        previous = time
        out.append(total)
    return tuple(out)


SHAPED_TIMES = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0)
SHAPED_FORWARDS = (0.0455, 0.0430, 0.0395, 0.0370, 0.0385, 0.0420, 0.0445, 0.0460)
"""An easing cycle that turns: the sign change is where a naive cubic would
overshoot and where the monotone-convex regions earn their keep."""


@pytest.fixture(scope="module")
def shaped() -> MonotoneConvex:
    """A monotone convex interpolant on a curve that bends both ways."""
    return MonotoneConvex(SHAPED_TIMES, _log_dfs(SHAPED_TIMES, SHAPED_FORWARDS))


class TestDiscreteForwards:
    """Step 0: the inputs the method is built from."""

    def test_they_invert_the_construction(self):
        log_dfs = _log_dfs(SHAPED_TIMES, SHAPED_FORWARDS)
        assert discrete_forwards(SHAPED_TIMES, log_dfs) == pytest.approx(SHAPED_FORWARDS)

    def test_the_first_interval_runs_from_zero(self):
        forwards = discrete_forwards((2.0,), (-0.08,))
        assert forwards == pytest.approx((0.04,))

    def test_misaligned_inputs_refuse(self):
        with pytest.raises(ValueError):
            discrete_forwards((1.0, 2.0), (-0.04,))

    def test_no_nodes_refuse(self):
        with pytest.raises(ValueError):
            discrete_forwards((), ())

    def test_a_non_increasing_grid_refuses(self):
        with pytest.raises(ValueError):
            discrete_forwards((1.0, 1.0), (-0.04, -0.08))

    def test_a_non_positive_first_time_refuses(self):
        with pytest.raises(ValueError):
            discrete_forwards((0.0, 1.0), (0.0, -0.04))


class TestNodeForwards:
    """Steps 1 and 2: the instantaneous forwards, and the collar on them."""

    def test_there_is_one_more_than_there_are_intervals(self, shaped):
        assert len(shaped.nodes) == len(SHAPED_TIMES) + 1

    def test_an_interior_node_is_the_time_weighted_blend(self):
        times = (1.0, 3.0)
        discrete = (0.04, 0.05)
        forwards = node_forwards(times, discrete, collar=False)
        # The node at t=1 sits between intervals of length 1 and 2; Hagan-West
        # weights each discrete forward by the *other* interval's length.
        assert forwards[1] == pytest.approx((1.0 / 3.0) * 0.05 + (2.0 / 3.0) * 0.04)

    def test_a_flat_curve_stays_flat(self):
        times = (1.0, 2.0, 3.0, 4.0)
        forwards = node_forwards(times, (0.04,) * 4)
        assert forwards == pytest.approx((0.04,) * 5)

    def test_the_collar_holds_every_node_inside_twice_its_neighbours(self, shaped):
        discrete, nodes = shaped.discrete, shaped.nodes
        assert 0.0 <= nodes[0] <= 2.0 * discrete[0]
        for index in range(1, len(discrete)):
            ceiling = 2.0 * min(discrete[index - 1], discrete[index])
            assert 0.0 <= nodes[index] <= ceiling
        assert 0.0 <= nodes[-1] <= 2.0 * discrete[-1]

    def test_the_collar_is_what_enforces_positivity(self):
        """A sharp drop makes the uncollared endpoint estimate go negative.
        With the collar it cannot; that is PRD-002 AC-5.2's mechanism, not a
        coincidence of this curve."""
        times = (0.5, 1.0, 1.5)
        discrete = (0.002, 0.06, 0.06)
        loose = node_forwards(times, discrete, collar=False)
        tight = node_forwards(times, discrete, collar=True)
        assert min(loose) < 0.0, "the fixture no longer exercises the collar"
        assert min(tight) >= 0.0

    def test_a_single_interval_needs_no_blend(self):
        assert node_forwards((1.0,), (0.04,)) == pytest.approx((0.04, 0.04))


class TestPositivity:
    """PRD-002 AC-5.2: positive inputs give positive interpolated forwards."""

    @pytest.mark.parametrize(
        "forwards",
        [
            (0.04,) * 6,
            (0.01, 0.02, 0.03, 0.04, 0.05, 0.06),
            (0.06, 0.05, 0.04, 0.03, 0.02, 0.01),
            (0.05, 0.001, 0.05, 0.001, 0.05, 0.001),
            (0.0001, 0.08, 0.0001, 0.08, 0.0001, 0.08),
            (0.03, 0.031, 0.029, 0.045, 0.02, 0.04),
        ],
    )
    def test_the_interpolated_forward_never_goes_negative(self, forwards):
        times = tuple(0.5 * (k + 1) for k in range(len(forwards)))
        interpolant = MonotoneConvex(times, _log_dfs(times, forwards))
        grid = [times[-1] * k / 400.0 for k in range(401)]
        worst = min(interpolant.instantaneous_forward(t) for t in grid)
        assert worst >= -1e-15, f"forward reached {worst}"

    def test_the_sawtooth_really_does_stress_it(self):
        """Without the collar the same sawtooth goes negative, so the test
        above is not passing for want of anything to catch."""
        forwards = (0.05, 0.001, 0.05, 0.001, 0.05, 0.001)
        times = tuple(0.5 * (k + 1) for k in range(len(forwards)))
        loose = MonotoneConvex(times, _log_dfs(times, forwards), collar=False)
        grid = [times[-1] * k / 400.0 for k in range(401)]
        assert min(loose.instantaneous_forward(t) for t in grid) < 0.0

    def test_discount_factors_stay_decreasing(self, shaped):
        grid = [SHAPED_TIMES[-1] * k / 200.0 for k in range(201)]
        values = [shaped.log_df(t) for t in grid]
        assert all(b <= a + 1e-15 for a, b in zip(values, values[1:], strict=False))


class TestNodeReproduction:
    """The interpolant passes through the data it was given."""

    def test_it_reproduces_every_node_log_df(self, shaped):
        for time, log_df in zip(SHAPED_TIMES, shaped.log_dfs, strict=True):
            assert shaped.log_df(time) == pytest.approx(log_df, abs=1e-13)

    def test_it_reproduces_every_discrete_forward(self, shaped):
        """The integral of the instantaneous forward over each interval has to
        come back to the average forward — this is the property that makes the
        calibration invariant to the interpolation."""
        grid = (0.0, *SHAPED_TIMES)
        for index, expected in enumerate(shaped.discrete):
            left, right = grid[index], grid[index + 1]
            integral = -(shaped.log_df(right) - shaped.log_df(left))
            assert integral / (right - left) == pytest.approx(expected, abs=1e-12)

    def test_log_df_at_zero_is_zero(self, shaped):
        assert shaped.log_df(0.0) == 0.0
        assert shaped.log_df(-1.0) == 0.0

    def test_past_the_last_node_the_forward_is_held_flat(self, shaped):
        tail = shaped.nodes[-1]
        assert shaped.instantaneous_forward(SHAPED_TIMES[-1] + 3.0) == pytest.approx(tail)
        beyond = SHAPED_TIMES[-1] + 2.0
        assert shaped.log_df(beyond) == pytest.approx(
            shaped.log_dfs[-1] - tail * 2.0
        )

    @pytest.mark.parametrize("region_forwards", [
        (0.04, 0.041, 0.042),      # gentle, region 1
        (0.04, 0.001, 0.04),       # sharp down then up
        (0.001, 0.05, 0.001),      # sharp up then down
        (0.04, 0.04, 0.04),        # exactly flat, the g == 0 degeneracy
        (0.02, 0.04, 0.04),        # one end exactly on the discrete forward
    ])
    def test_every_region_still_reproduces_its_nodes(self, region_forwards):
        times = tuple(float(k + 1) for k in range(len(region_forwards)))
        interpolant = MonotoneConvex(times, _log_dfs(times, region_forwards))
        for time, log_df in zip(times, interpolant.log_dfs, strict=True):
            assert interpolant.log_df(time) == pytest.approx(log_df, abs=1e-13)


class TestCurveIntegration:
    """The interpolation reaches the curve object it is declared on."""

    @pytest.fixture
    def nodes(self) -> tuple[date, ...]:
        return tuple(AS_OF + timedelta(days=round(365 * t)) for t in SHAPED_TIMES)

    @pytest.fixture
    def pair(self, nodes) -> tuple[DiscountCurve, DiscountCurve]:
        log_dfs = _log_dfs(SHAPED_TIMES, SHAPED_FORWARDS)
        dfs = tuple(math.exp(v) for v in log_dfs)
        return (
            DiscountCurve(AS_OF, nodes, dfs, "log_linear_df"),
            DiscountCurve(AS_OF, nodes, dfs, "monotone_convex"),
        )

    def test_both_interpolations_are_declarable(self):
        assert set(INTERPOLATIONS) == {"log_linear_df", "monotone_convex"}

    def test_they_agree_at_every_node(self, pair, nodes):
        linear, convex = pair
        for node in nodes:
            assert convex.df(node) == pytest.approx(linear.df(node), rel=1e-13)

    def test_they_disagree_between_nodes_in_the_forward(self, pair, nodes):
        linear, convex = pair
        midpoint = nodes[4] + (nodes[5] - nodes[4]) // 2
        gap = abs(
            convex.instantaneous_forward(midpoint) - linear.instantaneous_forward(midpoint)
        )
        assert gap > 1e-5, "the two interpolations have stopped differing"

    def test_the_log_linear_forward_is_a_step_function(self, pair, nodes):
        linear, _ = pair
        inside = [nodes[4] + timedelta(days=d) for d in (10, 200, 500)]
        values = [linear.instantaneous_forward(d) for d in inside]
        assert values[0] == pytest.approx(values[1])

    def test_an_unknown_interpolation_refuses(self, nodes):
        log_dfs = _log_dfs(SHAPED_TIMES, SHAPED_FORWARDS)
        with pytest.raises(UnsupportedConventionError) as excinfo:
            DiscountCurve(AS_OF, nodes, tuple(math.exp(v) for v in log_dfs), "cubic_spline")
        assert "not implemented" in str(excinfo.value)

    def test_the_interpolation_is_serialised(self, pair):
        _, convex = pair
        assert convex.to_dict()["interpolation"] == "monotone_convex"


class TestComparison:
    """PRD-002 AC-5.1: bootstrap the same strip both ways and report the gap."""

    @pytest.fixture
    def instruments(self):
        """A quarterly futures strip on a curve that is not flat."""
        start = AS_OF + timedelta(days=14)
        stub = RealizedStubNode(end=start, accrual_factor=1.0 + 0.043 * (14 / 360.0))
        nodes = [stub]
        period = start
        for index, forward in enumerate(
            (0.0430, 0.0405, 0.0380, 0.0360, 0.0355, 0.0365, 0.0385, 0.0400)
        ):
            following = period + timedelta(days=91)
            nodes.append(
                FuturesNode(
                    start=period,
                    end=following,
                    forward_rate=forward,
                    label=f"SR3-{index + 1}",
                )
            )
            period = following
        return tuple(nodes)

    @pytest.fixture
    def comparison(self, instruments):
        return compare_interpolations(AS_OF, instruments, step_days=7)

    def test_both_reprice_every_instrument(self, comparison):
        assert comparison.max_residual_bp < 0.01, (
            f"worst residual {comparison.max_residual_bp:.6f} bp"
        )

    def test_the_forward_difference_is_reported_and_real(self, comparison):
        assert comparison.max_forward_difference_bp > 0.0
        assert comparison.max_forward_difference_date in comparison.sample_dates

    def test_the_discount_factors_barely_differ(self, comparison):
        """The disagreement is local: it is in the shape of the forward inside
        an interval, and integrating over the interval cancels it. So the DF
        gap must be far smaller than the forward gap suggests."""
        assert comparison.max_df_difference < 1e-4
        assert comparison.max_df_difference * 1e4 < comparison.max_forward_difference_bp

    def test_both_curves_agree_at_the_calibrated_nodes(self, comparison):
        for node in comparison.log_linear.nodes:
            assert comparison.monotone_convex.df(node) == pytest.approx(
                comparison.log_linear.df(node), rel=1e-12
            )

    def test_the_two_curves_declare_what_they_are(self, comparison):
        assert comparison.log_linear.interpolation == "log_linear_df"
        assert comparison.monotone_convex.interpolation == "monotone_convex"

    def test_the_evidence_chains_both_bootstraps(self, comparison):
        assert len(comparison.evidence.sources) == 2
        assert comparison.evidence.fields["interpolations"] == [
            "log_linear_df",
            "monotone_convex",
        ]

    def test_the_evidence_says_why_they_can_differ(self, comparison):
        assert "no instrument constrains" in comparison.evidence.fields["note"]

    def test_the_payload_reports_the_difference(self, comparison):
        payload = comparison.payload_fields()
        assert payload["max_forward_difference_bp"] == pytest.approx(
            comparison.max_forward_difference_bp
        )
        assert payload["max_residual_bp"] == pytest.approx(comparison.max_residual_bp)
        json.dumps(comparison.to_dict())

    def test_the_scan_covers_the_curve(self, comparison):
        assert comparison.sample_dates[0] == AS_OF + timedelta(days=7)
        assert comparison.sample_dates[-1] <= comparison.log_linear.nodes[-1]
        assert len(comparison.sample_dates) > 50

    def test_it_is_deterministic(self, instruments):
        first = compare_interpolations(AS_OF, instruments, step_days=7)
        second = compare_interpolations(AS_OF, instruments, step_days=7)
        assert first.max_forward_difference_bp == second.max_forward_difference_bp
        assert first.log_linear.dfs == second.log_linear.dfs
