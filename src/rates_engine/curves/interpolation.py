"""Monotone convex interpolation, after Hagan and West (2006).

*Interpolation Methods for Curve Construction*, Applied Mathematical Finance
13(2). The method exists because the obvious alternatives misbehave in ways
that matter: linear interpolation on zero rates produces a sawtooth of
instantaneous forwards, and a natural cubic spline produces forwards that
oscillate and can go negative between nodes even when every input forward is
positive.

Monotone convex fixes the discrete forwards — the average forward over each
node interval, which is what the market quotes — and builds a continuous
instantaneous forward that **integrates back to them exactly**. That last
property is the reason it can be swapped for log-linear without disturbing a
calibration: every input instrument still reprices, because the quantity each
instrument pins is preserved by construction, not by the fit.

The algorithm in three steps:

1. Estimate an instantaneous forward at each node by interpolating the
   discrete forwards either side of it, with one-sided rules at the ends.
2. Collar those estimates into ``[0, 2 min(f_i, f_{i+1})]``. This is what
   makes positive inputs give positive outputs, which is PRD-002 AC-5.2.
3. On each interval fit a quadratic in ``g = f - f_discrete`` chosen from four
   regions, each of which is the one shape that stays monotone there.

The four regions are the whole of the method's subtlety and each is written
out below with the condition that selects it. The integral of each is in
closed form, so discount factors come from arithmetic rather than quadrature.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MonotoneConvex", "discrete_forwards", "node_forwards"]


def discrete_forwards(times: tuple[float, ...], log_dfs: tuple[float, ...]) -> tuple[float, ...]:
    """Average forward over each node interval, from log discount factors.

    Args:
        times: Node times in years, strictly increasing and all positive.
        log_dfs: ``ln P(t)`` at those nodes, aligned with ``times``.

    Returns:
        One forward per interval, the first covering ``(0, times[0]]``.

    Raises:
        ValueError: The inputs do not align, or a time is not positive and
            increasing.
    """
    if len(times) != len(log_dfs):
        raise ValueError(f"{len(times)} times and {len(log_dfs)} log discount factors")
    if not times:
        raise ValueError("monotone convex interpolation needs at least one node")
    if times[0] <= 0.0 or any(b <= a for a, b in zip(times, times[1:], strict=False)):
        raise ValueError(f"node times must be positive and increasing, got {times}")
    forwards = [-log_dfs[0] / times[0]]
    for index in range(1, len(times)):
        forwards.append(
            -(log_dfs[index] - log_dfs[index - 1]) / (times[index] - times[index - 1])
        )
    return tuple(forwards)


def node_forwards(
    times: tuple[float, ...], discrete: tuple[float, ...], *, collar: bool = True
) -> tuple[float, ...]:
    """Instantaneous forwards at the nodes, including time zero.

    Steps 1 and 2 of the method. The collar in step 2 is what carries
    positivity from the inputs to the output: an instantaneous forward held
    inside ``[0, 2 min(f_i, f_{i+1})]`` cannot drive the quadratic on either
    neighbouring interval below zero.

    Args:
        times: Node times in years.
        discrete: Average forward per interval, from :func:`discrete_forwards`.
        collar: Apply the positivity collar. Turning it off is for showing,
            in a test, that it is what does the work.

    Returns:
        ``len(times) + 1`` forwards, the first at time zero.
    """
    grid = (0.0, *times)
    count = len(discrete)
    interior: list[float] = []
    for index in range(1, count):
        left, middle, right = grid[index - 1], grid[index], grid[index + 1]
        span = right - left
        interior.append(
            (middle - left) / span * discrete[index] + (right - middle) / span * discrete[index - 1]
        )
    first = discrete[0] - 0.5 * (interior[0] - discrete[0]) if interior else discrete[0]
    last = (
        discrete[-1] - 0.5 * (interior[-1] - discrete[-1]) if interior else discrete[0]
    )
    forwards = [first, *interior, last]

    if collar:
        forwards[0] = min(max(0.0, forwards[0]), 2.0 * discrete[0])
        for index in range(1, count):
            forwards[index] = min(
                max(0.0, forwards[index]),
                2.0 * min(discrete[index - 1], discrete[index]),
            )
        forwards[count] = min(max(0.0, forwards[count]), 2.0 * discrete[-1])
    return tuple(forwards)


def _region(g0: float, g1: float) -> int:
    """Which of Hagan and West's four shapes keeps this interval monotone.

    Returns:
        ``1`` to ``4``. Region 4 is the fallback where both ends pull the
        same way and a single quadratic cannot stay monotone, so the interval
        is split at an interior point.
    """
    if g0 == 0.0 and g1 == 0.0:
        return 1
    # Region 4 splits the interval at eta = g1 / (g0 + g1) and centres it on
    # A = -g0 g1 / (g0 + g1). Both are degenerate when either end is exactly
    # zero: eta collapses onto an endpoint and A vanishes. The limit of the
    # region-4 shape there is g == 0 across the interval, whose integral
    # agrees with the plain quadratic's, so the node discount factors are
    # unaffected either way. Handled in the two functions below rather than
    # here, because the region is still 4 — it is the formula that degenerates,
    # not the classification.
    if (g0 < 0.0 and -0.5 * g0 <= g1 <= -2.0 * g0) or (
        g0 > 0.0 and -0.5 * g0 >= g1 >= -2.0 * g0
    ):
        return 1
    if (g0 < 0.0 and g1 > -2.0 * g0) or (g0 > 0.0 and g1 < -2.0 * g0):
        return 2
    if (g0 > 0.0 and 0.0 > g1 > -0.5 * g0) or (g0 < 0.0 and 0.0 < g1 < -0.5 * g0):
        return 3
    return 4


def _g_integral(g0: float, g1: float, x: float) -> float:
    """``integral of g from 0 to x``, in closed form for whichever region applies.

    Args:
        g0: Deviation of the instantaneous forward from the interval's
            discrete forward, at the left end.
        g1: The same at the right end.
        x: Position in the interval, in ``[0, 1]``.

    Returns:
        The integral, which is what turns an instantaneous forward into a
        discount factor without quadrature.
    """
    if x <= 0.0:
        return 0.0
    region = _region(g0, g1)
    if region == 1:
        return g0 * (x - 2.0 * x * x + x**3) + g1 * (-x * x + x**3)
    if region == 2:
        eta = (g1 + 2.0 * g0) / (g1 - g0)
        if x <= eta:
            return g0 * x
        return g0 * x + (g1 - g0) * (x - eta) ** 3 / (3.0 * (1.0 - eta) ** 2)
    if region == 3:
        eta = 3.0 * g1 / (g1 - g0)
        if x < eta:
            return g1 * x + (g0 - g1) * (eta**3 - (eta - x) ** 3) / (3.0 * eta * eta)
        at_eta = g1 * eta + (g0 - g1) * eta / 3.0
        return at_eta + g1 * (x - eta)
    if g0 == 0.0 or g1 == 0.0:
        return 0.0
    eta = g1 / (g1 + g0)
    amplitude = -g0 * g1 / (g0 + g1)
    if x < eta:
        return amplitude * x + (g0 - amplitude) * (eta**3 - (eta - x) ** 3) / (
            3.0 * eta * eta
        )
    at_eta = amplitude * eta + (g0 - amplitude) * eta / 3.0
    return (
        at_eta
        + amplitude * (x - eta)
        + (g1 - amplitude) * (x - eta) ** 3 / (3.0 * (1.0 - eta) ** 2)
    )


def _g_value(g0: float, g1: float, x: float) -> float:
    """``g(x)`` itself, for reporting the instantaneous forward."""
    region = _region(g0, g1)
    if region == 1:
        return g0 * (1.0 - 4.0 * x + 3.0 * x * x) + g1 * (-2.0 * x + 3.0 * x * x)
    if region == 2:
        eta = (g1 + 2.0 * g0) / (g1 - g0)
        if x <= eta:
            return g0
        return g0 + (g1 - g0) * ((x - eta) / (1.0 - eta)) ** 2
    if region == 3:
        eta = 3.0 * g1 / (g1 - g0)
        if x < eta:
            return g1 + (g0 - g1) * ((eta - x) / eta) ** 2
        return g1
    if g0 == 0.0 or g1 == 0.0:
        return 0.0
    eta = g1 / (g1 + g0)
    amplitude = -g0 * g1 / (g0 + g1)
    if x < eta:
        return amplitude + (g0 - amplitude) * ((eta - x) / eta) ** 2
    return amplitude + (g1 - amplitude) * ((x - eta) / (1.0 - eta)) ** 2


@dataclass(frozen=True)
class MonotoneConvex:
    """A monotone convex interpolant over one set of nodes.

    Attributes:
        times: Node times in years, increasing and positive.
        log_dfs: ``ln P(t)`` at those nodes.
        collar: Whether the positivity collar is applied.
    """

    times: tuple[float, ...]
    log_dfs: tuple[float, ...]
    collar: bool = True

    @property
    def discrete(self) -> tuple[float, ...]:
        """Average forward over each interval."""
        return discrete_forwards(self.times, self.log_dfs)

    @property
    def nodes(self) -> tuple[float, ...]:
        """Instantaneous forward at time zero and at each node."""
        return node_forwards(self.times, self.discrete, collar=self.collar)

    def log_df(self, time: float) -> float:
        """``ln P(t)`` at any time.

        Args:
            time: Time in years from the valuation date, non-negative.

        Returns:
            The log discount factor. Past the last node the final
            instantaneous forward is held flat, matching what the log-linear
            interpolant does there.
        """
        if time <= 0.0:
            return 0.0
        grid = (0.0, *self.times)
        discrete, node = self.discrete, self.nodes
        if time >= self.times[-1]:
            tail = node[-1]
            return self.log_dfs[-1] - tail * (time - self.times[-1])

        total = 0.0
        for index in range(1, len(grid)):
            left, right = grid[index - 1], grid[index]
            span = right - left
            f_d = discrete[index - 1]
            g0, g1 = node[index - 1] - f_d, node[index] - f_d
            if time >= right:
                total += f_d * span + span * _g_integral(g0, g1, 1.0)
                continue
            x = (time - left) / span
            total += f_d * (time - left) + span * _g_integral(g0, g1, x)
            break
        return -total

    def instantaneous_forward(self, time: float) -> float:
        """The instantaneous forward at a time, for inspecting smoothness.

        Args:
            time: Time in years, non-negative.

        Returns:
            The instantaneous forward rate as a decimal.
        """
        grid = (0.0, *self.times)
        discrete, node = self.discrete, self.nodes
        if time >= self.times[-1]:
            return node[-1]
        for index in range(1, len(grid)):
            left, right = grid[index - 1], grid[index]
            if left <= time < right:
                f_d = discrete[index - 1]
                g0, g1 = node[index - 1] - f_d, node[index] - f_d
                return f_d + _g_value(g0, g1, (time - left) / (right - left))
        return node[-1]  # pragma: no cover - covered by the tail branch above
