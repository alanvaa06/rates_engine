"""The discount curve: discount factors, the rates they imply, and how it bumps.

Interpolation is log-linear in the discount factor, which is the same thing as
piecewise-constant instantaneous forwards. That choice is why the futures
bootstrap in :mod:`rates_engine.curves.bootstrap` is exact rather than
approximate: a SOFR future's reference period is priced by the ratio of two
discount factors, and a piecewise-constant forward reproduces that ratio with
no interpolation error inside the period.

**Bumping.** Every risk number in this package comes from
:meth:`DiscountCurve.shifted`, which shifts the *continuously compounded zero
curve* by a function of time and rebuilds the discount factors as
``P(t) * exp(-s(t) * t)``. One primitive serves the parallel shift, the tent
shocks of the key-rate profile and the shock table, so those three cannot
silently disagree about what a basis point is.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.interpolation import MonotoneConvex
from rates_engine.errors import CurveArbitrageError, UnsupportedConventionError
from rates_engine.money import Currency, require_same_currency

__all__ = ["DiscountCurve", "CurveSet", "CURVE_TIME_BASIS"]

CURVE_TIME_BASIS = DayCount.ACT_365F
"""The curve's internal time axis. Deliberately not a money-market basis: the
interpolation variable should not inherit ACT/360's 365/360 stretch."""

_COMPOUNDING = ("continuous", "annual", "simple")

INTERPOLATIONS = ("log_linear_df", "monotone_convex")
"""The interpolations a curve may declare.

``log_linear_df``
    Piecewise-constant instantaneous forwards. The v1 default, and the reason
    the futures bootstrap is exact: a futures period is priced by a ratio of
    discount factors, which a constant forward reproduces with no
    interpolation error inside the period.
``monotone_convex``
    Hagan and West (2006). Continuous forwards that still integrate back to
    the discrete ones exactly, so every calibration instrument reprices
    either way and the two can be compared on the same fit.
"""


@dataclass(frozen=True)
class DiscountCurve:
    """Discount factors on a date grid, log-linearly interpolated.

    Attributes:
        as_of: Valuation date. ``df(as_of)`` is exactly 1.
        nodes: Node dates, strictly increasing and all after ``as_of``.
        dfs: Discount factors at those nodes, positive and non-increasing.
        interpolation: Always ``"log_linear_df"`` in v1; carried so that every
            exported curve states it rather than leaving it to be assumed.
        currency: What the curve discounts. Defaults to USD, which is what
            every curve in v1 and v2 was without saying so. Discounting a
            cashflow in another currency on it raises rather than returning
            a number nobody can interpret.
    """

    as_of: date
    nodes: tuple[date, ...]
    dfs: tuple[float, ...]
    interpolation: str = "log_linear_df"
    currency: Currency = Currency.USD

    def __post_init__(self) -> None:
        if len(self.nodes) != len(self.dfs):
            raise ValueError(f"{len(self.nodes)} nodes and {len(self.dfs)} discount factors")
        if not self.nodes:
            raise ValueError("a discount curve needs at least one node")
        if self.interpolation not in INTERPOLATIONS:
            raise UnsupportedConventionError(
                f"interpolation {self.interpolation!r} is not implemented; "
                f"supported: {', '.join(INTERPOLATIONS)}"
            )
        previous_date = self.as_of
        for node, df in zip(self.nodes, self.dfs, strict=True):
            if node <= previous_date:
                raise ValueError(f"curve nodes must increase past {previous_date}, got {node}")
            if not df > 0.0:
                raise CurveArbitrageError(
                    f"discount factor at {node} is {df!r}; a non-positive discount factor "
                    "is not a curve under any rate environment"
                )
            previous_date = node

    @property
    def rising_segments(self) -> tuple[tuple[date, date], ...]:
        """Segments where the discount factor increases, i.e. the forward is negative.

        Empty for an ordinary curve. Non-empty is not automatically an error:
        negative rates exist, and a risk bump can push a near-zero forward
        through zero without anything being wrong. What it is not allowed to
        be is a *bootstrapped* curve, which is checked in
        :func:`~rates_engine.curves.bootstrap.bootstrap_discount_curve`.
        """
        rising: list[tuple[date, date]] = []
        previous_date, previous_df = self.as_of, 1.0
        for node, df in zip(self.nodes, self.dfs, strict=True):
            if df > previous_df:
                rising.append((previous_date, node))
            previous_date, previous_df = node, df
        return tuple(rising)

    def require_monotone(self, context: str = "this curve") -> None:
        """Refuse if any segment has a rising discount factor.

        Called on a curve that came out of a calibration, where a rising
        segment means the quotes disagree rather than that rates are
        negative. Not called on a bumped curve, because a symmetric shock
        around a one basis point forward legitimately produces one.

        Args:
            context: What to name in the message, e.g. the calibration that
                produced the curve.

        Raises:
            CurveArbitrageError: A segment rises, named by its endpoints.
        """
        rising = self.rising_segments
        if rising:
            start, end = rising[0]
            raise CurveArbitrageError(
                f"{context}: the discount factor rises from {start} to {end}, so that "
                "segment implies a negative zero rate the inputs do not support"
            )

    @property
    def node_times(self) -> tuple[float, ...]:
        """Node dates as year fractions from ``as_of`` on :data:`CURVE_TIME_BASIS`."""
        return tuple(year_fraction(self.as_of, n, CURVE_TIME_BASIS) for n in self.nodes)

    def time(self, day: date) -> float:
        """Year fraction from ``as_of`` to ``day`` on the curve's internal basis."""
        return year_fraction(self.as_of, day, CURVE_TIME_BASIS)

    def df(self, day: date) -> float:
        """The discount factor for ``day``.

        Args:
            day: Any date. On or before ``as_of`` the answer is 1: this curve
                discounts, it does not accrue history.

        Returns:
            The discount factor. Between nodes the log of the factor is linear
            in time; past the last node the last segment's instantaneous
            forward is held flat, which keeps forwards continuous at the edge
            rather than introducing a kink where the data runs out.
        """
        if day <= self.as_of:
            return 1.0
        target = self.time(day)
        if self.interpolation == "monotone_convex":
            return math.exp(self._monotone_convex.log_df(target))
        times, logs = self.node_times, self._log_dfs
        if target <= times[0]:
            return math.exp(logs[0] * target / times[0]) if times[0] else 1.0
        if target >= times[-1]:
            if len(times) == 1:
                return math.exp(logs[-1] * target / times[-1])
            slope = (logs[-1] - logs[-2]) / (times[-1] - times[-2])
            return math.exp(logs[-1] + slope * (target - times[-1]))
        for left in range(len(times) - 1):
            if times[left] <= target <= times[left + 1]:
                span = times[left + 1] - times[left]
                weight = (target - times[left]) / span
                return math.exp(logs[left] * (1.0 - weight) + logs[left + 1] * weight)
        raise AssertionError("unreachable: target lies inside the node range")  # pragma: no cover

    @property
    def _log_dfs(self) -> tuple[float, ...]:
        return tuple(math.log(df) for df in self.dfs)

    @property
    def _monotone_convex(self) -> MonotoneConvex:
        """The Hagan-West interpolant over this curve's nodes."""
        return MonotoneConvex(self.node_times, self._log_dfs)

    def instantaneous_forward(self, day: date) -> float:
        """The instantaneous forward rate at a date, as a decimal a year.

        The quantity the two interpolations actually differ in: log-linear
        holds it constant across each interval and jumps at the nodes, while
        monotone convex makes it continuous. Discount factors at the nodes
        are identical either way, so this is where a comparison has to look.

        Args:
            day: The date to evaluate at.

        Returns:
            The instantaneous forward on the curve's own time basis.
        """
        target = self.time(day)
        if self.interpolation == "monotone_convex":
            return self._monotone_convex.instantaneous_forward(target)
        times, logs = self.node_times, self._log_dfs
        if target <= times[0]:
            return -logs[0] / times[0]
        for left in range(len(times) - 1):
            if times[left] <= target < times[left + 1]:
                return -(logs[left + 1] - logs[left]) / (times[left + 1] - times[left])
        if len(times) == 1:
            return -logs[0] / times[0]
        return -(logs[-1] - logs[-2]) / (times[-1] - times[-2])

    def zero(
        self,
        day: date,
        *,
        compounding: str = "continuous",
        day_count: DayCount = CURVE_TIME_BASIS,
    ) -> float:
        """The zero-coupon rate to ``day``, as a decimal.

        Args:
            day: Maturity.
            compounding: ``"continuous"``, ``"annual"`` or ``"simple"``.
            day_count: Basis the rate is quoted on. Changing it changes the
                number, which is why no rate leaves this package without one.

        Returns:
            The rate as a decimal.

        Raises:
            UnsupportedConventionError: ``compounding`` is not one of the three.
            ValueError: ``day`` is not after ``as_of``, so no rate is defined.
        """
        if compounding not in _COMPOUNDING:
            raise UnsupportedConventionError(
                f"compounding {compounding!r} is not implemented; supported: {_COMPOUNDING}"
            )
        tau = year_fraction(self.as_of, day, day_count)
        if tau <= 0.0:
            raise ValueError(f"zero rate needs a maturity after {self.as_of}, got {day}")
        df = self.df(day)
        if compounding == "continuous":
            return -math.log(df) / tau
        if compounding == "annual":
            return df ** (-1.0 / tau) - 1.0
        return (1.0 / df - 1.0) / tau

    def forward(
        self,
        start: date,
        end: date,
        *,
        day_count: DayCount = DayCount.ACT_360,
        compounding: str = "simple",
    ) -> float:
        """The forward rate over ``[start, end]``, as a decimal.

        Args:
            start: Start of the forward period.
            end: End of it.
            day_count: Basis for the period's year fraction. ACT/360 by
                default, which is the SOFR basis a futures period uses.
            compounding: ``"simple"``, ``"annual"`` or ``"continuous"``.

        Returns:
            The forward rate as a decimal.

        Raises:
            UnsupportedConventionError: ``compounding`` is not implemented.
            ValueError: ``end`` is not after ``start``.
        """
        if compounding not in _COMPOUNDING:
            raise UnsupportedConventionError(
                f"compounding {compounding!r} is not implemented; supported: {_COMPOUNDING}"
            )
        tau = year_fraction(start, end, day_count)
        if tau <= 0.0:
            raise ValueError(f"forward period must be positive, got {start} to {end}")
        growth = self.df(start) / self.df(end)
        if compounding == "simple":
            return (growth - 1.0) / tau
        if compounding == "annual":
            return growth ** (1.0 / tau) - 1.0
        return math.log(growth) / tau

    def shifted(self, shift: Callable[[float], float] | float) -> DiscountCurve:
        """A copy with the continuously compounded zero curve shifted.

        Args:
            shift: Either a constant shift in decimal rate — ``1e-4`` for a
                parallel basis point — or a function of time in years
                returning one, which is how the tent shocks of the key-rate
                profile are applied.

        Returns:
            A new curve; the original is untouched. ``P'(t) = P(t) exp(-s(t) t)``,
            so a positive shift lowers discount factors, as a rate rise should.

            No monotonicity check is applied. Shifting a one basis point
            forward down by a basis point takes it through zero, and a
            symmetric difference has to be able to do that or the DV01 of a
            near-zero curve would be undefined — which is the wrong answer in
            a currency that has had negative rates.
        """
        if callable(shift):
            function: Callable[[float], float] = shift
        else:
            constant = float(shift)

            def function(_time: float) -> float:
                return constant

        times = self.node_times
        return DiscountCurve(
            as_of=self.as_of,
            nodes=self.nodes,
            dfs=tuple(
                df * math.exp(-function(t) * t) for df, t in zip(self.dfs, times, strict=True)
            ),
            interpolation=self.interpolation,
        )

    def with_node(self, node: date, df: float) -> DiscountCurve:
        """A copy with one node appended or replaced.

        Args:
            node: Node date. Must be after the last existing node, or equal to
                it, in which case its factor is replaced.
            df: The discount factor there.

        Returns:
            The extended curve.

        Raises:
            ValueError: ``node`` falls before the last existing node, which
                would mean the bootstrap solved its instruments out of order.
        """
        if self.nodes and node < self.nodes[-1]:
            raise ValueError(
                f"node {node} precedes the last node {self.nodes[-1]}; "
                "bootstrap instruments must be solved in maturity order"
            )
        if self.nodes and node == self.nodes[-1]:
            return DiscountCurve(self.as_of, self.nodes, self.dfs[:-1] + (df,), self.interpolation)
        return DiscountCurve(
            self.as_of, self.nodes + (node,), self.dfs + (df,), self.interpolation
        )

    def to_dict(self) -> dict[str, object]:
        """Serialise nodes, factors and the interpolation that joins them."""
        return {
            "as_of": self.as_of.isoformat(),
            "interpolation": self.interpolation,
            "currency": self.currency.value,
            "time_basis": CURVE_TIME_BASIS.value,
            "nodes": [n.isoformat() for n in self.nodes],
            "discount_factors": list(self.dfs),
        }


@dataclass(frozen=True)
class CurveSet:
    """The curves a valuation needs: one to discount with, one to project from.

    Attributes:
        discount: The OIS-SOFR curve. Collateralised flows discount here
            because the collateral is remunerated at SOFR — the Fujii-Shimada-
            Takahashi and Piterbarg argument — and nowhere else.
        tenor: The projection curve for a term rate, when there is one. ``None``
            means single-curve: projection falls back to the discount curve,
            which is the correct answer when the basis is zero.
    """

    discount: DiscountCurve
    tenor: DiscountCurve | None = None

    def __post_init__(self) -> None:
        if self.tenor is not None:
            require_same_currency(
                self.discount.currency,
                self.tenor.currency,
                operation="a curve set",
            )

    @property
    def currency(self) -> Currency:
        """What this set values in. Its two curves are checked to agree."""
        return self.discount.currency

    @property
    def projection(self) -> DiscountCurve:
        """The curve floating legs project off: :attr:`tenor` if set, else :attr:`discount`."""
        return self.tenor if self.tenor is not None else self.discount

    @property
    def is_dual(self) -> bool:
        """True when a distinct projection curve is present."""
        return self.tenor is not None

    @property
    def as_of(self) -> date:
        """Valuation date, taken from the discount curve."""
        return self.discount.as_of

    def shifted(self, shift: Callable[[float], float] | float) -> CurveSet:
        """Shift both curves by the same amount, for parallel and tent bumps."""
        return CurveSet(
            self.discount.shifted(shift),
            self.tenor.shifted(shift) if self.tenor is not None else None,
        )
