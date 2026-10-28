"""Curves with a shape imposed on them, rather than bent through every quote.

Two models, for two different reasons.

**Nelson-Siegel** (1987) describes a yield curve with four numbers — a level,
a slope, a curvature and the maturity where the curvature peaks. It will not
reproduce every quote, and that is the point: the residual is a measurement
of how much of the curve is shape and how much is idiosyncratic. Fitted here
the way the estimation literature does it, which matters more than it sounds:
for a fixed decay the model is **linear in the three betas**, so those come
from least squares in closed form and only the decay is searched over. Throwing
all four at a nonlinear optimiser is the standard way to get a fit that
depends on where the search started. The one-dimensional search has the same
problem on a smaller scale -- the profile over the decay can have more than
one minimum -- so it scans a grid before it refines.

**The FOMC step curve** (Heitfield and Park) says something the smooth models
cannot: the overnight rate does not drift, it sits still and then jumps on
eight scheduled days a year. Fitting a piecewise-constant path to SR1 and SR3
recovers what the futures strip implies each meeting will do. The contracts
are priced by their exchange rules: SR3 compounds ``1 + r_i d_i / 360`` over
the business days of its quarter, ``d_i`` the calendar days rate ``r_i``
applies for (three for a Friday), and SR1 averages every calendar day at the
rate of the business day on or before it. A contract already in progress
settles partly on fixings that are history by ``as_of``, and those come from
the fixings, not the path.

**The term rate it implies is not a swap rate.** A step curve fitted to
futures gives an expected average overnight rate, and a term rate quoted in
the market embeds a convexity adjustment this curve has not applied. The
gap is small inside a year and grows after; every result here carries that
warning rather than leaving it to be remembered.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
from scipy.optimize import least_squares, minimize_scalar

from rates_engine.conventions.calendar import SIFMA_US, HolidayCalendar
from rates_engine.conventions.daycount import CURVE_TIME_BASIS, year_fraction
from rates_engine.conventions.indices import SOFR
from rates_engine.core.errors import (
    CalibrationError,
    ImplausibleInputError,
    MissingFixingError,
    UnderdeterminedCurveError,
    UnsupportedConventionError,
)
from rates_engine.core.evidence import DataQuality, Degradation, Evidence
from rates_engine.core.money import Currency
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import DiscountCurve
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = [
    "NelsonSiegel",
    "NelsonSiegelFit",
    "fit_nelson_siegel",
    "FOMCStepCurve",
    "FOMCStepFit",
    "fit_fomc_step_curve",
    "TERM_RATE_CAVEAT",
]

TERM_RATE_CAVEAT = (
    "A term rate read off a curve fitted to futures carries no convexity "
    "adjustment: it is the expected average overnight rate, not a traded term "
    "rate. The two agree closely inside a year and diverge after it, because "
    "the adjustment grows with the square of maturity."
)

_TAU_GRID_POINTS = 100
"""Log-spaced decays the Nelson-Siegel profile is scanned at before Brent refines.

The profile -- the least-squares error as a function of the decay alone, the
betas solved in closed form at each -- can have an interior minimum and a
lower value on a bound, or two interior minima. Bounded Brent on the whole
range finds one of them, and which one depends on the range. On 300 random
Svensson curves it was more than 5 bp of RMSE worse than a 4,000-point grid
on 24 of them. A hundred points, then Brent on the two grid cells around the
best, matched or beat that grid on all 300, deterministically.
"""

_AT_BOUND_RELATIVE = 1e-6
"""How close to a bound, relative to it, a fitted decay counts as sitting on it."""

_FUTURES_SYMBOLS = ("SR1", "SR3")
"""The contracts the step fit knows how to settle: SR1 averages, SR3 compounds."""


@dataclass(frozen=True)
class NelsonSiegel:
    """The Nelson-Siegel zero curve, as four numbers.

    ``z(t) = b0 + b1 (1 - e^-x)/x + b2 [(1 - e^-x)/x - e^-x]``, ``x = t / tau``

    Attributes:
        beta0: The level, and the asymptotic long rate.
        beta1: The slope. ``beta0 + beta1`` is the instantaneous short rate.
        beta2: The curvature, a hump whose height it sets.
        tau: The decay in years, setting where the hump sits.
    """

    beta0: float
    beta1: float
    beta2: float
    tau: float

    def __post_init__(self) -> None:
        if self.tau <= 0.0:
            raise ValueError(f"tau must be positive, got {self.tau!r}")

    @staticmethod
    def loadings(time: float, tau: float) -> tuple[float, float, float]:
        """The three factor loadings at a maturity.

        Args:
            time: Maturity in years, non-negative.
            tau: Decay parameter in years.

        Returns:
            Loadings on level, slope and curvature. At zero maturity the
            slope loading is one and the curvature loading zero, which is the
            limit rather than a special case.
        """
        if time <= 0.0:
            return 1.0, 1.0, 0.0
        x = time / tau
        decay = math.exp(-x)
        slope = -math.expm1(-x) / x
        return 1.0, slope, slope - decay

    def zero_rate(self, time: float) -> float:
        """The continuously compounded zero rate at a maturity, as a decimal.

        Args:
            time: Maturity in years.

        Returns:
            The zero rate.
        """
        level, slope, curve = self.loadings(time, self.tau)
        return self.beta0 * level + self.beta1 * slope + self.beta2 * curve

    @property
    def short_rate(self) -> float:
        """The instantaneous short rate, ``beta0 + beta1``."""
        return self.beta0 + self.beta1

    def to_dict(self) -> dict[str, Any]:
        """Serialise the four parameters."""
        return {
            "beta0": self.beta0,
            "beta1": self.beta1,
            "beta2": self.beta2,
            "tau": self.tau,
            "short_rate": self.short_rate,
        }

    def discount_curve(
        self, as_of: date, nodes: tuple[date, ...], *, currency: Currency = Currency.USD
    ) -> DiscountCurve:
        """Sample the model onto a discount curve.

        Args:
            as_of: Valuation date.
            nodes: Dates to sample at.

        Returns:
            A :class:`~rates_engine.curves.discount.DiscountCurve` carrying
            the sampled factors. Between nodes it interpolates rather than
            re-evaluating the model, so sample finely enough for the use.
        """
        times = [year_fraction(as_of, n, CURVE_TIME_BASIS) for n in nodes]
        return DiscountCurve(
            as_of,
            nodes,
            tuple(math.exp(-self.zero_rate(t) * t) for t in times),
            currency=currency,
        )


@dataclass(frozen=True)
class NelsonSiegelFit(EngineResult):
    """A fitted Nelson-Siegel curve and how far it missed.

    Attributes:
        model: The fitted :class:`NelsonSiegel`.
        times: Maturities fitted, in years.
        observed: Zero rates fitted to, as decimals.
        fitted: What the model says at the same maturities.
        rmse_bp: Root mean squared error in basis points.
        max_error_bp: Largest single miss, in basis points.
    """

    model: NelsonSiegel
    times: tuple[float, ...]
    observed: tuple[float, ...]
    fitted: tuple[float, ...]
    rmse_bp: float
    max_error_bp: float

    def payload_fields(self) -> dict[str, Any]:
        """The parameters, the data and the errors."""
        return {
            "curve_kind": "parametric",
            "model": "nelson_siegel",
            "parameters": self.model.to_dict(),
            "times": list(self.times),
            "observed_bp": [v * 1e4 for v in self.observed],
            "fitted_bp": [v * 1e4 for v in self.fitted],
            "rmse_bp": self.rmse_bp,
            "max_error_bp": self.max_error_bp,
        }


def fit_nelson_siegel(
    times: tuple[float, ...],
    zero_rates: tuple[float, ...],
    *,
    tau_bounds: tuple[float, float] = (0.05, 30.0),
    tolerance_bp: float | None = None,
) -> NelsonSiegelFit:
    """Fit Nelson-Siegel to a set of zero rates.

    Concentrated least squares: for a fixed ``tau`` the model is linear in the
    betas, so those come from a closed-form solve and the search runs over one
    bounded parameter. This is both faster and, more to the point,
    deterministic — a four-way nonlinear search lands in different local
    minima from different starting points and produces betas that jump
    between refits while the fit quality barely moves.

    The one-parameter search is a grid and then a refinement: the profile is
    evaluated at :data:`_TAU_GRID_POINTS` log-spaced decays across
    ``tau_bounds`` and bounded Brent polishes the best of them. Brent alone,
    on the whole range, could settle in the wrong one of two minima -- on one
    Svensson curve it stopped on the bound at tau = 30 with 7.16 bp RMSE
    while tau = 2.01 gives 1.60 bp.

    Args:
        times: Maturities in years, at least four, all finite.
        zero_rates: Continuously compounded zero rates as decimals, all finite.
        tau_bounds: Range to search the decay over, in years; positive and
            increasing. A fit that lands on either end is returned with a
            ``nelson_siegel_tau_at_bound`` warning, because the decay was then
            set by the range rather than by the data.
        tolerance_bp: When set, refuse a fit whose RMSE exceeds this, in
            basis points.

    Returns:
        The :class:`NelsonSiegelFit`.

    Raises:
        CalibrationError: Fewer than four maturities, mismatched lengths, a
            fit whose error is not a finite number, or an RMSE above
            ``tolerance_bp``.
        ImplausibleInputError: A time or a rate is NaN or infinite.
        ValueError: ``tau_bounds`` is not a positive, increasing pair.
    """
    if len(times) != len(zero_rates):
        raise CalibrationError(f"{len(times)} times and {len(zero_rates)} rates do not align")
    if len(times) < 4:
        raise CalibrationError(
            f"Nelson-Siegel has four parameters and this is {len(times)} observations. "
            "A fit through as many points as parameters is interpolation wearing a "
            "model's name."
        )
    # A NaN used to pass straight through: the fit came back NaN, and
    # ``rmse > tolerance`` is false for a NaN, so even an explicit tolerance
    # never fired.
    for name, values in (("times", times), ("zero_rates", zero_rates)):
        bad = [index for index, value in enumerate(values) if not math.isfinite(value)]
        if bad:
            raise ImplausibleInputError(
                f"Nelson-Siegel {name} at positions {bad} are not finite numbers "
                f"({[values[i] for i in bad]}); drop the maturities that have no quote"
            )
    lower, upper = (float(b) for b in tau_bounds)
    if not (math.isfinite(lower) and math.isfinite(upper) and 0.0 < lower < upper):
        raise ValueError(
            f"tau_bounds must be a positive, increasing pair of years, got {tau_bounds!r}"
        )
    observations = np.asarray(zero_rates, dtype=float)

    def betas_for(tau: float) -> tuple[np.ndarray, float]:
        design = np.array([NelsonSiegel.loadings(t, tau) for t in times])
        # Overflow is not hidden here: it makes the error infinite, which the
        # profile treats as no fit at that decay and the RMSE check below
        # refuses if it survives to the answer.
        with np.errstate(over="ignore", invalid="ignore"):
            solution, *_ = np.linalg.lstsq(design, observations, rcond=None)
            residual = design @ solution - observations
            error = float(residual @ residual)
        return solution, error if math.isfinite(error) else math.inf

    grid = np.exp(np.linspace(math.log(lower), math.log(upper), _TAU_GRID_POINTS))
    grid[0], grid[-1] = lower, upper
    profile = [betas_for(float(t))[1] for t in grid]
    best = int(np.argmin(profile))
    if not math.isfinite(profile[best]):
        raise CalibrationError(
            "Nelson-Siegel has no finite fit anywhere in tau_bounds: the squared error is "
            "not a finite number at any decay, so the rates are too large to fit "
            f"(largest magnitude {max(abs(r) for r in zero_rates):.3e})"
        )
    search = minimize_scalar(
        lambda tau: betas_for(tau)[1],
        bounds=(float(grid[max(best - 1, 0)]), float(grid[min(best + 1, len(grid) - 1)])),
        method="bounded",
        options={"xatol": 1e-10},
    )
    tau = float(search.x)
    if betas_for(tau)[1] > profile[best]:
        # Brent never evaluates the ends of its bracket, so on a bound the
        # grid point itself can be the better of the two.
        tau = float(grid[best])
    betas, _ = betas_for(tau)
    model = NelsonSiegel(float(betas[0]), float(betas[1]), float(betas[2]), tau)

    fitted = tuple(model.zero_rate(t) for t in times)
    errors_bp = [(f - o) * 1e4 for f, o in zip(fitted, zero_rates, strict=True)]
    rmse_bp = math.sqrt(sum(e * e for e in errors_bp) / len(errors_bp))
    max_error_bp = max(abs(e) for e in errors_bp)
    if not math.isfinite(rmse_bp):
        raise CalibrationError(
            f"Nelson-Siegel fitted to an RMSE that is not a finite number ({rmse_bp!r} bp); "
            "a fit whose error cannot be measured is not a fit"
        )
    if tolerance_bp is not None and not rmse_bp <= tolerance_bp:
        raise CalibrationError(
            f"Nelson-Siegel fitted to {rmse_bp:.4f} bp RMSE, above the {tolerance_bp} bp "
            f"tolerance; worst maturity off by {max_error_bp:.4f} bp"
        )

    at_bound: str | None = None
    if tau - lower <= _AT_BOUND_RELATIVE * lower:
        at_bound = "lower"
    elif upper - tau <= _AT_BOUND_RELATIVE * upper:
        at_bound = "upper"
    warnings: tuple[Degradation, ...] = ()
    if at_bound is not None:
        edge = lower if at_bound == "lower" else upper
        warnings = (
            Degradation(
                code="nelson_siegel_tau_at_bound",
                message=(
                    f"The decay tau = {tau:.6g} years sits on the {at_bound} bound "
                    f"({edge} of tau_bounds {list(tau_bounds)}): the best fit lies at or "
                    "beyond the search range, so tau was set by the range rather than the "
                    "data. Widen tau_bounds if the range was not a deliberate constraint."
                ),
                data_quality=DataQuality.ASSUMED,
            ),
        )

    evidence = Evidence(
        produced_by="curves.fit_nelson_siegel",
        fields={
            "curve_kind": "parametric",
            "model": "nelson_siegel",
            "estimator": "concentrated_least_squares",
            "estimator_note": (
                "Betas solved in closed form for each tau; the tau profile scanned on a "
                f"{_TAU_GRID_POINTS}-point log grid across tau_bounds and refined by bounded "
                "Brent around the best point, so a second minimum cannot capture the "
                "search. Deterministic, unlike a four-way nonlinear search."
            ),
            "tau_bounds": list(tau_bounds),
            "tau_grid_points": _TAU_GRID_POINTS,
            "tau_at_bound": at_bound,
            "observations": len(times),
            "parameters": model.to_dict(),
            "rmse_bp": rmse_bp,
            "max_error_bp": max_error_bp,
        },
        warnings=warnings,
    )
    return NelsonSiegelFit(
        evidence=evidence,
        model=model,
        times=tuple(times),
        observed=tuple(zero_rates),
        fitted=fitted,
        rmse_bp=rmse_bp,
        max_error_bp=max_error_bp,
    )


@dataclass(frozen=True)
class FOMCStepCurve:
    """An overnight rate that sits still and jumps on meeting days.

    Attributes:
        as_of: Valuation date; the first segment starts here.
        meeting_dates: Effective dates of the rate changes, increasing. A
            segment runs from each one to the next.
        segment_rates: The overnight rate in each segment, as decimals. One
            more than there are meetings.
    """

    as_of: date
    meeting_dates: tuple[date, ...]
    segment_rates: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.segment_rates) != len(self.meeting_dates) + 1:
            raise ValueError(
                f"{len(self.meeting_dates)} meetings need {len(self.meeting_dates) + 1} "
                f"segment rates, got {len(self.segment_rates)}"
            )
        if any(b <= a for a, b in zip(self.meeting_dates, self.meeting_dates[1:], strict=False)):
            raise ValueError("meeting dates must be strictly increasing")

    def rate_on(self, day: date) -> float:
        """The overnight rate in force on a date, as a decimal.

        Args:
            day: The date.

        Returns:
            The segment rate covering it.
        """
        index = 0
        for meeting in self.meeting_dates:
            if day >= meeting:
                index += 1
            else:
                break
        return self.segment_rates[index]

    def compounded(
        self, start: date, end: date, *, calendar: HolidayCalendar = SIFMA_US
    ) -> float:
        """Compounded rate over a period by the CME and New York Fed rule, as a decimal.

        ``(prod(1 + r_i d_i / 360) - 1) * 360 / D``: one factor per business
        day ``i`` of the period, ``r_i`` the path's rate on it and ``d_i`` the
        calendar days it applies for, three for a Friday. ``D`` is the
        period's calendar days. This used to compound ``1 + r/360`` once per
        calendar day, which turns each weekend into ``(1 + r/360)^3`` rather
        than ``1 + 3r/360`` -- between 0.03 and 0.04 bp on a quarter.

        Args:
            start: First accrual day, inclusive. If it is not a business day
                its rate is the previous business day's, as for a fixing.
            end: Last, exclusive.
            calendar: The business days the rate is published on; SIFMA for
                SOFR.

        Returns:
            The compounded rate. This is SR3's settlement basis.

        Raises:
            ValueError: ``end`` does not follow ``start``.
        """
        if end <= start:
            raise ValueError(f"end {end} must follow start {start}")
        return _settlement_rate(
            "SR3", _rate_days(start, end, calendar), self.rate_on, (end - start).days
        )

    def averaged(
        self, start: date, end: date, *, calendar: HolidayCalendar = SIFMA_US
    ) -> float:
        """Calendar-day arithmetic average over a period by the CME rule, as a decimal.

        Every calendar day counts once, at the rate of the business day on or
        before it.

        Args:
            start: First accrual day, inclusive.
            end: Last, exclusive.
            calendar: The business days the rate is published on.

        Returns:
            The average rate. This is SR1's settlement basis.

        Raises:
            ValueError: ``end`` does not follow ``start``.
        """
        if end <= start:
            raise ValueError(f"end {end} must follow start {start}")
        return _settlement_rate(
            "SR1", _rate_days(start, end, calendar), self.rate_on, (end - start).days
        )

    def term_rate(
        self, start: date, months: int, *, calendar: HolidayCalendar = SIFMA_US
    ) -> float:
        """The compounded overnight rate over a term, with the caveat attached.

        Not a traded term rate: see :data:`TERM_RATE_CAVEAT`. The caller gets
        the number; the evidence in :class:`FOMCStepFit` carries the warning.

        Args:
            start: Start of the term.
            months: Term length in whole months.
            calendar: The business days the overnight rate compounds on.

        Returns:
            The compounded rate as a decimal.
        """
        from rates_engine.conventions.schedule import add_months

        return self.compounded(start, add_months(start, months), calendar=calendar)

    def discount_curve(
        self,
        nodes: tuple[date, ...],
        *,
        currency: Currency = Currency.USD,
        calendar: HolidayCalendar = SIFMA_US,
    ) -> DiscountCurve:
        """Sample the step path onto a discount curve.

        Each node's factor is ``1 / prod(1 + r_i d_i / 360)`` from ``as_of``,
        the same rule :meth:`compounded` applies and from the same terms,
        :func:`_rate_days`, laid out once to the last node. The business day
        open at a node accrues only the calendar days up to it.

        Args:
            nodes: Dates to sample at, all after ``as_of``.
            currency: What the curve discounts.
            calendar: The business days the rate compounds on.

        Returns:
            The discount curve implied by compounding the path.
        """
        factors: list[float] = []
        terms = _rate_days(self.as_of, nodes[-1], calendar) if nodes else []
        closed, term, opened = 1.0, 0, self.as_of
        for node in nodes:
            # Close every term that ends on or before the node; the one left
            # open accrues the days from its start up to the node.
            while term < len(terms) and opened + timedelta(days=terms[term][1]) <= node:
                anchor, covered = terms[term]
                closed *= 1.0 + self.rate_on(anchor) * covered / 360.0
                opened += timedelta(days=covered)
                term += 1
            partial = (node - opened).days
            open_factor = (
                1.0 + self.rate_on(terms[term][0]) * partial / 360.0 if partial > 0 else 1.0
            )
            factors.append(1.0 / (closed * open_factor))
        return DiscountCurve(self.as_of, nodes, tuple(factors), currency=currency)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the meeting dates and the path between them."""
        return {
            "as_of": self.as_of.isoformat(),
            "meeting_dates": [d.isoformat() for d in self.meeting_dates],
            "segment_rates": list(self.segment_rates),
            "segment_changes_bp": [
                (b - a) * 1e4
                for a, b in zip(self.segment_rates, self.segment_rates[1:], strict=False)
            ],
        }


@dataclass(frozen=True)
class FOMCStepFit(EngineResult):
    """A step path fitted to a futures strip.

    Attributes:
        curve: The fitted :class:`FOMCStepCurve`.
        residuals_bp: Repricing residual per contract label, in basis points.
        max_residual_bp: The worst of them.
        implied_moves_bp: Rate change implied at each meeting, in basis points.
    """

    curve: FOMCStepCurve
    residuals_bp: dict[str, float]
    max_residual_bp: float
    implied_moves_bp: dict[str, float]

    def payload_fields(self) -> dict[str, Any]:
        """The path, the moves it implies and how well it fitted."""
        return {
            "curve_kind": "parametric",
            "model": "fomc_step",
            "curve": self.curve.to_dict(),
            "residuals_bp": dict(self.residuals_bp),
            "max_residual_bp": self.max_residual_bp,
            "implied_moves_bp": dict(self.implied_moves_bp),
            "term_rate_caveat": TERM_RATE_CAVEAT,
        }


def _anchor(day: date, calendar: HolidayCalendar) -> date:
    """The business day whose published rate a calendar day takes: itself or the one before."""
    return day if calendar.is_business_day(day) else calendar.previous_business_day(day)


def _rate_days(start: date, end: date, calendar: HolidayCalendar) -> list[tuple[date, int]]:
    """``(business day i, d_i)`` over ``[start, end)``: CME's terms, in order.

    Every calendar day takes the rate of the business day on or before it, so
    a period opening on a non-business day opens on the previous business
    day's rate, and ``d_i`` counts the calendar days each business day's rate
    covers inside the period -- three for an ordinary Friday, and cut short
    by ``end``.
    """
    anchor = _anchor(start, calendar)
    out: list[tuple[date, int]] = []
    covered, day = 0, start
    while day < end:
        if day != anchor and calendar.is_business_day(day):
            out.append((anchor, covered))
            anchor, covered = day, 0
        covered += 1
        day += timedelta(days=1)
    out.append((anchor, covered))
    return out


def _settlement_rate(
    symbol: str,
    terms: Sequence[tuple[date, int]],
    rate_for: Callable[[date], float],
    days: int,
) -> float:
    """SR1's average or SR3's compounded rate over ``terms`` from :func:`_rate_days`.

    Args:
        symbol: ``"SR1"`` to average, anything else to compound.
        terms: ``(business day, d_i)`` pairs.
        rate_for: The overnight rate published for a business day, decimal.
        days: Calendar days in the period, ``D``.

    Returns:
        The settlement rate as a decimal: ``sum(r_i d_i) / D`` or
        ``(prod(1 + r_i d_i / 360) - 1) * 360 / D``.
    """
    if symbol == "SR1":
        return sum(rate_for(day) * count for day, count in terms) / days
    factor = 1.0
    for day, count in terms:
        factor *= 1.0 + rate_for(day) * count / 360.0
    return (factor - 1.0) / (days / 360.0)


@dataclass(frozen=True)
class _PricedContract:
    """One contract of the strip, its terms laid out once and its fixed days resolved.

    Attributes:
        label: The contract's label.
        symbol: ``"SR1"`` or ``"SR3"``.
        days: Calendar days in the reference period.
        terms: ``(business day, d_i)`` over the period.
        fixed: Published rate for each business day the path does not price:
            every one before ``as_of``, and ``as_of`` itself when its fixing
            is in the snapshot.
        realised_days: Calendar days priced from ``fixed``.
        forecast_days: Calendar days on the ``as_of`` fixing that the path
            priced because there was no snapshot, or it did not hold that
            day yet.
        missing_from: The first business day before ``as_of`` with no fixing,
            or ``None`` when every one has one.
    """

    label: str
    symbol: str
    days: int
    terms: tuple[tuple[date, int], ...]
    fixed: Mapping[date, float]
    realised_days: int
    forecast_days: int
    missing_from: date | None

    @classmethod
    def build(
        cls,
        contract: tuple[str, date, date, str, float],
        as_of: date,
        series: Series | None,
        calendar: HolidayCalendar,
    ) -> _PricedContract:
        """Lay out a contract against ``as_of`` and the fixings available.

        The convention on ``as_of`` is QuantLib's default: a fixing dated
        ``as_of`` is read when it is there and forecast when it is not. One
        dated before it is history and has to be there.
        """
        label, begin, end, symbol, _price = contract
        if end <= begin:
            raise ValueError(f"contract {label!r}: end {end} must follow start {begin}")
        terms = tuple(_rate_days(begin, end, calendar))
        fixed: dict[date, float] = {}
        missing: date | None = None
        realised = forecast = 0
        for day, count in terms:
            if day > as_of:
                continue
            value = None if series is None else series.get(day)
            if value is not None:
                fixed[day] = float(value)
                realised += count
            elif day == as_of:
                forecast += count
            else:
                missing = missing or day
        return cls(
            label, symbol.upper(), (end - begin).days, terms, fixed, realised, forecast, missing
        )

    @property
    def priced_by_path(self) -> tuple[date, ...]:
        """The business days whose rate this contract takes from the path."""
        return tuple(day for day, _ in self.terms if day not in self.fixed)

    def rate(self, path: FOMCStepCurve) -> float:
        """The settlement rate under ``path``, fixings first, as a decimal."""
        fixed = self.fixed
        return _settlement_rate(
            self.symbol,
            self.terms,
            lambda day: fixed[day] if day in fixed else path.rate_on(day),
            self.days,
        )


def fit_fomc_step_curve(
    as_of: date,
    meeting_dates: tuple[date, ...],
    contracts: tuple[tuple[str, date, date, str, float], ...],
    *,
    tolerance_bp: float = 1.0,
    strict: bool = True,
    fixings: MarketSnapshot | None = None,
    calendar: HolidayCalendar = SIFMA_US,
) -> FOMCStepFit:
    """Fit a piecewise-constant overnight path to SR1 and SR3 settlements.

    Each contract is priced by its exchange rule (see the module docstring),
    one business day at a time. A business day before ``as_of`` is history:
    its rate is the published fixing from ``fixings``, not the path, which
    starts at ``as_of``. The front SR1 is in progress on every day but the
    first of its month, so this is the ordinary case rather than an edge.
    Reading those days off the first segment instead -- what this did
    before -- prices a month that straddled a meeting already held as if the
    meeting had not happened, and bends the first segment to absorb it. The
    fixing dated ``as_of`` itself is read when the snapshot has it and
    forecast by the path when it does not, QuantLib's default. Not yet
    published is the ordinary state of that fixing during the day, so the
    forecast lowers no quality: ``forecast_days_on_as_of`` in the evidence
    counts, per contract, the calendar days that rest on it.

    **Every segment must be pinned, and pinned on its own.** A meeting on or
    before ``as_of`` is refused: the rate it set is in the fixings, and the
    segment before it covers only days already fixed, so nothing pins it --
    ``least_squares`` returned it at its starting point and reported a move
    for a meeting already held. The same holds for a segment the strip never
    reaches, after a meeting beyond the last contract. Touching every
    segment is not enough either: the strip has to determine each rate, not
    only some combinations of them. Two quarters that both sit in the last
    segment pin one thing, not two, and five contracts can then leave the
    first three segments free to trade off against each other with every
    price unchanged; the fit found a point on that line and passed
    ``strict`` with a move of +1172 bp. The strip's contract-by-segment
    weights, from its days still to come, must have full column rank.

    Args:
        as_of: Valuation date, where the first segment starts.
        meeting_dates: Effective dates of the scheduled rate decisions.
        contracts: One tuple per contract: label, reference start, reference
            end, ``"SR1"`` or ``"SR3"``, and the settlement price (100 minus
            the rate in percent).
        tolerance_bp: Repricing tolerance in basis points.
        strict: Refuse when any contract misses the tolerance.
        fixings: A snapshot holding the published SOFR fixings as the
            series ``"SOFR"``, decimals by business day -- the same
            ``MarketSnapshot`` swaps and caps take. The series is required
            only when a contract prices a business day on or before
            ``as_of``, and then every such day before ``as_of`` must be in
            it; ``as_of`` itself is read if it is there and forecast if not.
        calendar: The business days the overnight rate is published on.

    Returns:
        The :class:`FOMCStepFit`. Its evidence records how many calendar days
        of each contract were priced from fixings, and how many rest on a
        forecast of the ``as_of`` fixing.

    Raises:
        UnderdeterminedCurveError: Fewer contracts than segments, a meeting
            on or before ``as_of``, a segment no contract prices a day of, or
            segments the strip prices only in combinations it cannot
            separate -- named in the message.
        UnsupportedConventionError: A contract is neither SR1 nor SR3.
        ImplausibleInputError: A price or a fixing is NaN or infinite.
        MissingFixingError: A contract covers a business day before ``as_of``
            and there is no fixing for it or no ``fixings`` at all; or a
            contract covers a business day on or before ``as_of`` and the
            snapshot has no ``"SOFR"`` series.
        CalibrationError: Under ``strict``, a contract missed the tolerance.
    """
    segments = len(meeting_dates) + 1
    held = [meeting for meeting in meeting_dates if meeting <= as_of]
    if held:
        raise UnderdeterminedCurveError(
            f"meetings {[m.isoformat() for m in held]} are on or before as_of "
            f"{as_of.isoformat()}: the rate each one set is already in the fixings, and the "
            "segment before it prices no day still to come, so nothing pins it. Drop them "
            "from meeting_dates; the path's first segment starts at as_of."
        )
    if len(contracts) < segments:
        raise UnderdeterminedCurveError(
            f"{len(contracts)} contracts cannot pin {segments} segments "
            f"({len(meeting_dates)} meetings plus the stub before the first). "
            "Extend the strip or drop the meetings it does not reach."
        )
    for label, _begin, _end, symbol, price in contracts:
        if symbol.upper() not in _FUTURES_SYMBOLS:
            raise UnsupportedConventionError(
                f"contract {label!r} is {symbol!r}; the step fit settles "
                f"{' and '.join(_FUTURES_SYMBOLS)} only, and anything else used to be "
                "compounded as an SR3 without a word"
            )
        if not math.isfinite(price):
            raise ImplausibleInputError(
                f"contract {label!r} has settlement price {price!r}, which is not a number "
                "to fit; drop the contract or supply its price"
            )
    series = _sofr_series(as_of, contracts, fixings, calendar)
    priced = [
        _PricedContract.build(contract, as_of, series, calendar) for contract in contracts
    ]
    bad = sorted(
        {day for p in priced for day, value in p.fixed.items() if not math.isfinite(value)}
    )
    if bad:
        raise ImplausibleInputError(
            f"{SOFR.name} fixings on {[d.isoformat() for d in bad]} are not finite numbers; "
            "a day without a fixing is left out of the series, not stored as NaN"
        )
    unfixed = [p for p in priced if p.missing_from is not None]
    if unfixed:
        raise MissingFixingError(
            "these contracts' periods started before "
            f"{as_of.isoformat()} and part of their settlement is already fixed: "
            + ", ".join(
                f"{p.label!r} (needs a fixing for {p.missing_from.isoformat()})"
                for p in unfixed
                if p.missing_from is not None
            )
            + f". Pass the published fixings as fixings=MarketSnapshot(as_of=..., series="
            f"{{{SOFR.name!r}: Series(...)}}); the path starts at as_of and does not know "
            "what the overnight rate did before it."
        )
    _require_every_segment_identified(as_of, meeting_dates, priced)

    targets = np.array([(100.0 - price) / 100.0 for *_, price in contracts])
    start = float(targets[0])

    def model_rates(rates: np.ndarray) -> np.ndarray:
        candidate = FOMCStepCurve(as_of, meeting_dates, tuple(float(r) for r in rates))
        return np.array([contract.rate(candidate) for contract in priced])

    solution = least_squares(
        lambda r: model_rates(r) - targets,
        np.full(segments, start),
        xtol=1e-15,
        ftol=1e-15,
        gtol=1e-15,
    )
    curve = FOMCStepCurve(as_of, meeting_dates, tuple(float(r) for r in solution.x))
    modelled = model_rates(solution.x)
    residuals = {
        contract[0]: float((modelled[i] - targets[i]) * 1e4)
        for i, contract in enumerate(contracts)
    }
    worst = max(abs(r) for r in residuals.values())
    if strict and worst > tolerance_bp:
        raise CalibrationError(
            f"the step curve reprices the strip to {worst:.4f} bp at worst, above the "
            f"{tolerance_bp} bp tolerance. Residuals: "
            f"{ {k: round(v, 4) for k, v in residuals.items()} }"
        )

    moves = {
        meeting.isoformat(): (curve.segment_rates[i + 1] - curve.segment_rates[i]) * 1e4
        for i, meeting in enumerate(meeting_dates)
    }
    evidence = Evidence(
        produced_by="curves.fit_fomc_step_curve",
        fields={
            "curve_kind": "parametric",
            "model": "fomc_step_heitfield_park",
            "meetings": len(meeting_dates),
            "segments": segments,
            "contracts": len(contracts),
            "residuals_bp": residuals,
            "max_residual_bp": worst,
            "implied_moves_bp": moves,
            "term_rate_caveat": TERM_RATE_CAVEAT,
            "convexity_adjustment_applied": False,
            "settlement_rules": {
                "SR1": "calendar-day average, each day at its business day's rate",
                "SR3": "prod(1 + r_i d_i / 360) over business days (CME, New York Fed)",
            },
            "calendar": calendar.name,
            "realised_days": {p.label: p.realised_days for p in priced},
            "forecast_days_on_as_of": {p.label: p.forecast_days for p in priced},
        },
        inputs=(
            (series.provenance,) if series is not None and any(p.fixed for p in priced) else ()
        ),
        warnings=(
            Degradation(
                code="term_rate_without_convexity",
                message=TERM_RATE_CAVEAT,
                data_quality=DataQuality.ASSUMED,
            ),
        ),
    )
    return FOMCStepFit(
        evidence=evidence,
        curve=curve,
        residuals_bp=residuals,
        max_residual_bp=worst,
        implied_moves_bp=moves,
    )


def _sofr_series(
    as_of: date,
    contracts: Sequence[tuple[str, date, date, str, float]],
    fixings: MarketSnapshot | None,
    calendar: HolidayCalendar,
) -> Series | None:
    """The snapshot's SOFR series, demanded only when a contract prices a day it holds.

    A contract needs published fixings when its first business day -- the one
    whose rate its first calendar day takes -- is on or before ``as_of``. A
    snapshot passed for anything else (Treasury yields, say) is not a demand
    for SOFR: asking every snapshot for the series refused strips that
    ``fixings=None`` fits. When a contract does need it and the series is
    missing or misnamed, the refusal stands: a snapshot is a statement that
    the fixings are in it, and forecasting a fixing it was meant to carry
    would hide the mistake.
    """
    if fixings is None:
        return None
    series = fixings.series.get(SOFR.name)
    if series is not None:
        return series
    needing = [
        (label, _anchor(begin, calendar))
        for label, begin, _end, _symbol, _price in contracts
        if _anchor(begin, calendar) <= as_of
    ]
    if needing:
        raise MissingFixingError(
            f"the snapshot has no {SOFR.name!r} series (it has {sorted(fixings.series)}), "
            "and these contracts settle partly on fixings published by as_of "
            f"{as_of.isoformat()}: "
            + ", ".join(f"{label!r} (from {day.isoformat()})" for label, day in needing)
            + f". Add the fixings as series {SOFR.name!r}. A fixing dated as_of is forecast "
            "only without a snapshot, or when the series is there and lacks that day."
        )
    return None


_NULL_SHARE = 1e-9
"""How much a direction the strip cannot see must move a segment to name it.

Directions of unit length; rounding leaves components around 1e-16 on the
segments a direction does not involve, and a real share is a ratio of day
counts. A segment moved less than this by every such direction is pinned
for any purpose a price quoted to a hundredth of a basis point can serve.
"""


def _require_every_segment_identified(
    as_of: date,
    meeting_dates: tuple[date, ...],
    priced: Sequence[_PricedContract],
) -> None:
    """Refuse a path whose free segments the strip does not determine one by one.

    Counting contracts is not enough: one whose period is wholly fixed pins
    nothing, and a meeting beyond the strip's last day leaves the segment
    after it untouched. ``least_squares`` returns such a segment at its
    starting value, with nothing in the fit to say so. Touching every
    segment is not enough either. The strip pins each rate only if its
    contract-by-segment weights -- the calendar days each contract prices
    from each segment, its fixed days left out -- have full column rank;
    otherwise some move of several rates together leaves every price where
    it was, and the solver returns an arbitrary point on that line.

    The weights are SR1's exactly and SR3's to first order: compounding
    scales each day's weight by a factor within a few parts in ten thousand
    of one, which moves no rank but a coincidence's. They are whole numbers
    of days, so a dependence among the columns is exact and leaves a singular
    value at rounding level. ``numpy.linalg.matrix_rank``'s default threshold
    is the largest singular value times the larger dimension times machine
    epsilon -- 1e-15 to 2e-15 of the largest for these strips -- and it sits
    far from both sides. The review's dependent strip has its smallest
    singular value at 1e-20 of the largest. The least well separated strip
    in the tests, where one day of one quarter tells a segment apart, keeps
    3.4e-3, and a full year of SR1 and SR3 against eight meetings keeps 0.21.
    """
    live = [p for p in priced if p.priced_by_path]
    segments = len(meeting_dates) + 1
    if len(live) < segments:
        raise UnderdeterminedCurveError(
            f"{len(live)} contracts price a day from the path, and the path has {segments} "
            "segments; the others are wholly fixed and pin nothing. Extend the strip or "
            "drop the meetings it does not reach."
        )
    bounds = (as_of, *meeting_dates)
    names = [
        f"{bounds[i].isoformat()} onwards"
        if i == segments - 1
        else f"{bounds[i].isoformat()} to {bounds[i + 1].isoformat()}"
        for i in range(segments)
    ]
    weights = np.zeros((len(live), segments))
    for row, contract in enumerate(live):
        for day, count in contract.terms:
            if day not in contract.fixed:
                weights[row, bisect_right(meeting_dates, day)] += count
    idle = [names[j] for j in range(segments) if not weights[:, j].any()]
    if idle:
        raise UnderdeterminedCurveError(
            f"no contract prices a day in the segments {idle}, so nothing pins their rate. "
            "Drop the meetings the strip does not reach, or add a contract that covers them."
        )
    rank = int(np.linalg.matrix_rank(weights))
    if rank < segments:
        _, _, directions = np.linalg.svd(weights)
        unseen = directions[rank:]
        tangled = [
            names[j] for j in range(segments) if float(np.abs(unseen[:, j]).max()) > _NULL_SHARE
        ]
        raise UnderdeterminedCurveError(
            f"the strip pins {rank} independent combinations of {segments} segment rates, not "
            f"each one: the segments {tangled} cannot be told apart, since some move of their "
            "rates together leaves every contract's price where it is. Contracts that price "
            "the same segments in the same proportions count once. Add a contract that "
            "prices one of them on its own -- an SR1 for a month between two meetings -- or "
            "drop a meeting."
        )
