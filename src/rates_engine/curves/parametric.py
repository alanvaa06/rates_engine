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
depends on where the search started.

**The FOMC step curve** (Heitfield and Park) says something the smooth models
cannot: the overnight rate does not drift, it sits still and then jumps on
eight scheduled days a year. Fitting a piecewise-constant path to SR1 and SR3
recovers what the futures strip implies each meeting will do.

**The term rate it implies is not a swap rate.** A step curve fitted to
futures gives an expected average overnight rate, and a term rate quoted in
the market embeds a convexity adjustment this curve has not applied. The
gap is small inside a year and grows after; every result here carries that
warning rather than leaving it to be remembered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
from scipy.optimize import minimize_scalar

from rates_engine.conventions.calendar import SIFMA_US, SIFMAUSCalendar
from rates_engine.conventions.daycount import year_fraction
from rates_engine.curves.discount import CURVE_TIME_BASIS, DiscountCurve
from rates_engine.errors import CalibrationError, UnderdeterminedCurveError
from rates_engine.evidence import DataQuality, Degradation, Evidence
from rates_engine.results import EngineResult

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

    def discount_curve(self, as_of: date, nodes: tuple[date, ...]) -> DiscountCurve:
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
            as_of, nodes, tuple(math.exp(-self.zero_rate(t) * t) for t in times)
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

    Args:
        times: Maturities in years, at least four.
        zero_rates: Continuously compounded zero rates as decimals.
        tau_bounds: Range to search the decay over, in years.
        tolerance_bp: When set, refuse a fit whose RMSE exceeds this.

    Returns:
        The :class:`NelsonSiegelFit`.

    Raises:
        CalibrationError: Fewer than four maturities, mismatched lengths, or
            an RMSE above ``tolerance_bp``.
    """
    if len(times) != len(zero_rates):
        raise CalibrationError(f"{len(times)} times and {len(zero_rates)} rates do not align")
    if len(times) < 4:
        raise CalibrationError(
            f"Nelson-Siegel has four parameters and this is {len(times)} observations. "
            "A fit through as many points as parameters is interpolation wearing a "
            "model's name."
        )
    observations = np.asarray(zero_rates, dtype=float)

    def betas_for(tau: float) -> tuple[np.ndarray, float]:
        design = np.array([NelsonSiegel.loadings(t, tau) for t in times])
        solution, *_ = np.linalg.lstsq(design, observations, rcond=None)
        residual = design @ solution - observations
        return solution, float(residual @ residual)

    search = minimize_scalar(
        lambda tau: betas_for(tau)[1], bounds=tau_bounds, method="bounded",
        options={"xatol": 1e-10},
    )
    tau = float(search.x)
    betas, _ = betas_for(tau)
    model = NelsonSiegel(float(betas[0]), float(betas[1]), float(betas[2]), tau)

    fitted = tuple(model.zero_rate(t) for t in times)
    errors_bp = [(f - o) * 1e4 for f, o in zip(fitted, zero_rates, strict=True)]
    rmse_bp = math.sqrt(sum(e * e for e in errors_bp) / len(errors_bp))
    max_error_bp = max(abs(e) for e in errors_bp)
    if tolerance_bp is not None and rmse_bp > tolerance_bp:
        raise CalibrationError(
            f"Nelson-Siegel fitted to {rmse_bp:.4f} bp RMSE, above the {tolerance_bp} bp "
            f"tolerance; worst maturity off by {max_error_bp:.4f} bp"
        )

    evidence = Evidence(
        produced_by="curves.fit_nelson_siegel",
        fields={
            "curve_kind": "parametric",
            "model": "nelson_siegel",
            "estimator": "concentrated_least_squares",
            "estimator_note": (
                "Betas solved in closed form for each tau, tau searched over a bounded "
                "interval. Deterministic, unlike a four-way nonlinear search."
            ),
            "tau_bounds": list(tau_bounds),
            "observations": len(times),
            "parameters": model.to_dict(),
            "rmse_bp": rmse_bp,
            "max_error_bp": max_error_bp,
        },
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

    def compounded(self, start: date, end: date) -> float:
        """Daily-compounded rate over a period, ACT/360, as a decimal.

        Args:
            start: First accrual day, inclusive.
            end: Last, exclusive.

        Returns:
            The compounded rate. This is SR3's settlement basis.

        Raises:
            ValueError: ``end`` does not follow ``start``.
        """
        if end <= start:
            raise ValueError(f"end {end} must follow start {start}")
        factor, day = 1.0, start
        while day < end:
            factor *= 1.0 + self.rate_on(day) / 360.0
            day += timedelta(days=1)
        return (factor - 1.0) / ((end - start).days / 360.0)

    def averaged(self, start: date, end: date) -> float:
        """Day-weighted arithmetic average over a period, as a decimal.

        Args:
            start: First accrual day, inclusive.
            end: Last, exclusive.

        Returns:
            The average rate. This is SR1's settlement basis.

        Raises:
            ValueError: ``end`` does not follow ``start``.
        """
        if end <= start:
            raise ValueError(f"end {end} must follow start {start}")
        total, day = 0.0, start
        while day < end:
            total += self.rate_on(day)
            day += timedelta(days=1)
        return total / (end - start).days

    def term_rate(
        self, start: date, months: int, *, calendar: SIFMAUSCalendar = SIFMA_US
    ) -> float:
        """The compounded overnight rate over a term, with the caveat attached.

        Not a traded term rate: see :data:`TERM_RATE_CAVEAT`. The caller gets
        the number; the evidence in :class:`FOMCStepFit` carries the warning.

        Args:
            start: Start of the term.
            months: Term length in whole months.
            calendar: Unused for the arithmetic, accepted so the signature
                does not change when a business-day term is added.

        Returns:
            The compounded rate as a decimal.
        """
        del calendar
        from rates_engine.conventions.schedule import add_months

        return self.compounded(start, add_months(start, months))

    def discount_curve(self, nodes: tuple[date, ...]) -> DiscountCurve:
        """Sample the step path onto a discount curve.

        Args:
            nodes: Dates to sample at, all after ``as_of``.

        Returns:
            The discount curve implied by compounding the path.
        """
        factors, running, previous = [], 1.0, self.as_of
        for node in nodes:
            day = previous
            while day < node:
                running *= 1.0 + self.rate_on(day) / 360.0
                day += timedelta(days=1)
            previous = node
            factors.append(1.0 / running)
        return DiscountCurve(self.as_of, nodes, tuple(factors))

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


def fit_fomc_step_curve(
    as_of: date,
    meeting_dates: tuple[date, ...],
    contracts: tuple[tuple[str, date, date, str, float], ...],
    *,
    tolerance_bp: float = 1.0,
    strict: bool = True,
) -> FOMCStepFit:
    """Fit a piecewise-constant overnight path to SR1 and SR3 settlements.

    Args:
        as_of: Valuation date, where the first segment starts.
        meeting_dates: Effective dates of the scheduled rate decisions.
        contracts: One tuple per contract: label, reference start, reference
            end, ``"SR1"`` or ``"SR3"``, and the settlement price.
        tolerance_bp: Repricing tolerance in basis points.
        strict: Refuse when any contract misses the tolerance.

    Returns:
        The :class:`FOMCStepFit`.

    Raises:
        UnderdeterminedCurveError: Fewer contracts than segments, so some
            segment is pinned by nothing.
        CalibrationError: Under ``strict``, a contract missed the tolerance.
    """
    segments = len(meeting_dates) + 1
    if len(contracts) < segments:
        raise UnderdeterminedCurveError(
            f"{len(contracts)} contracts cannot pin {segments} segments "
            f"({len(meeting_dates)} meetings plus the stub before the first). "
            "Extend the strip or drop the meetings it does not reach."
        )

    targets = np.array([(100.0 - price) / 100.0 for *_, price in contracts])
    start = float(targets[0])

    def model_rates(rates: np.ndarray) -> np.ndarray:
        candidate = FOMCStepCurve(as_of, meeting_dates, tuple(float(r) for r in rates))
        out = []
        for _, begin, end, symbol, _price in contracts:
            out.append(
                candidate.averaged(begin, end)
                if symbol.upper() == "SR1"
                else candidate.compounded(begin, end)
            )
        return np.array(out)

    from scipy.optimize import least_squares

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
        },
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
