"""A curve's cached arrays are exactly what recomputing them would give.

``DiscountCurve`` caches its node times and log discount factors, and the
curves derived from it -- one per root-solver trial in the bootstrap, one per
risk bump -- are handed those arrays rather than recomputing them. That is
only safe if a handed-down array is bit-identical to a fresh one, which is
what this file checks. A seeding bug would not raise; it would price on the
wrong times, so the check is on equality of the floats, not on closeness.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from rates_engine.conventions.daycount import year_fraction
from rates_engine.curves.discount import CURVE_TIME_BASIS, DiscountCurve

AS_OF = date(2026, 1, 15)


def _fresh(curve: DiscountCurve) -> tuple[tuple[float, ...], tuple[float, ...]]:
    times = tuple(year_fraction(curve.as_of, n, CURVE_TIME_BASIS) for n in curve.nodes)
    return times, tuple(math.log(df) for df in curve.dfs)


def _curve(gaps: list[int], rate: float, interpolation: str) -> DiscountCurve:
    nodes, day = [], AS_OF
    for gap in gaps:
        day += timedelta(days=gap)
        nodes.append(day)
    dfs = tuple(
        math.exp(-rate * year_fraction(AS_OF, n, CURVE_TIME_BASIS)) for n in nodes
    )
    return DiscountCurve(AS_OF, tuple(nodes), dfs, interpolation)


curves = st.builds(
    _curve,
    st.lists(st.integers(1, 400), min_size=1, max_size=12),
    st.floats(-0.01, 0.12),
    st.sampled_from(["log_linear_df", "monotone_convex"]),
)


@settings(max_examples=60, deadline=None)
@given(curves, st.integers(1, 400), st.floats(0.5, 1.0))
def test_an_appended_node_carries_exactly_the_fresh_arrays(curve, gap, ratio):
    extended = curve.with_node(curve.nodes[-1] + timedelta(days=gap), curve.dfs[-1] * ratio)
    assert (extended.node_times, extended._log_dfs) == _fresh(extended)


@settings(max_examples=60, deadline=None)
@given(curves, st.floats(0.5, 1.0))
def test_a_replaced_node_carries_exactly_the_fresh_arrays(curve, ratio):
    replaced = curve.with_node(curve.nodes[-1], curve.dfs[-1] * ratio)
    assert (replaced.node_times, replaced._log_dfs) == _fresh(replaced)


@settings(max_examples=60, deadline=None)
@given(curves, st.floats(-0.02, 0.02))
def test_a_shifted_curve_carries_exactly_the_fresh_arrays(curve, shift):
    bumped = curve.shifted(shift)
    assert (bumped.node_times, bumped._log_dfs) == _fresh(bumped)


def test_the_cache_does_not_enter_equality_or_hashing():
    # cached_property writes to the instance __dict__, which a frozen
    # dataclass's generated __eq__ and __hash__ never read. Two curves with
    # the same fields are equal whether or not either has been evaluated.
    first = _curve([90, 90, 90], 0.04, "monotone_convex")
    second = _curve([90, 90, 90], 0.04, "monotone_convex")
    first.df(AS_OF + timedelta(days=100))
    assert first == second
    assert hash(first) == hash(second)
