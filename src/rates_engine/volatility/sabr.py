"""SABR through Hagan's asymptotic expansion, and an honest account of its limits.

Hagan, Kumar, Lesniewski and Woodward, *Managing Smile Risk* (Wilmott, 2002).
The model is

    dF = alpha F^beta dW1,  d(alpha) = nu alpha dW2,  <dW1, dW2> = rho dt

and what is implemented here is not the model but its **asymptotic implied
volatility**: a small-``nu^2 T`` expansion that maps the four parameters to a
Black or Bachelier volatility in closed form. Two consequences follow, and
both are reported rather than assumed away.

*It is an approximation, not a solution.* The error is ``O(nu^2 T)``, so it
degrades with expiry and with vol-of-vol. At a ten-year expiry on a lively
smile the expansion and the model it approximates are visibly different
objects.

*It is not arbitrage-free.* For long expiries and low strikes the expansion
produces a smile whose implied risk-neutral density goes negative — a known
defect, not a bug in this implementation.
:func:`density_diagnostics` measures it by Breeden-Litzenberger and the result
carries the verdict, because a calibration that fits beautifully and implies
a negative density has told you something you need to know.

**Beta is fixed, not fitted** (PRD-002 decision 1). On a single smile, beta
and rho are close to unidentifiable: both tilt the backbone, so a joint fit is
ill-conditioned and returns parameters that jump between recalibrations while
the fit quality barely moves. Beta is chosen a priori — 0 for normal dynamics,
0.5 for the CIR-like convention, 1 for lognormal — and alpha, rho and nu are
calibrated.

Equations implemented, by their numbers in the paper: (2.17a) for the
lognormal volatility, (2.18) for its at-the-money limit, and (A.59a) with its
own at-the-money limit for the normal volatility.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import least_squares

from rates_engine.errors import (
    CalibrationError,
    ExpansionBreakdownError,
    ShiftRequiredError,
)
from rates_engine.evidence import Evidence
from rates_engine.results import EngineResult
from rates_engine.volatility.kinds import OptionKind

__all__ = [
    "SABRParameters",
    "SABRCalibration",
    "lognormal_vol",
    "normal_vol",
    "calibrate",
    "density_diagnostics",
    "expansion_is_valid",
    "DEFAULT_BETA",
    "Z_SERIES_THRESHOLD",
]

DEFAULT_BETA = 0.5
"""The CIR-like convention. Fixed rather than fitted; see the module docstring."""

_BREAKDOWN_PENALTY = 1.0
"""Residual returned for parameters where the expansion breaks down.

One whole unit of volatility — ten thousand basis points — so the optimiser
treats the region as a wall. A NaN would be a cliff: the step becomes
undefined and the search stops rather than turning around.
"""

Z_SERIES_THRESHOLD = 1e-7
"""Below this ``|z|``, ``z / x(z)`` is taken from its series rather than its ratio.

``x(z)`` vanishes with ``z``, so the ratio is 0/0 at the money and loses
digits either side of it. The series ``1 + rho z / 2`` is exact to the order
that matters and has no cancellation.
"""


@dataclass(frozen=True)
class SABRParameters:
    """One calibrated smile.

    Attributes:
        alpha: Level of instantaneous volatility, in the units implied by
            ``beta``. Strictly positive.
        beta: Backbone exponent in ``[0, 1]``. Fixed, not fitted.
        rho: Correlation between the rate and its volatility, in ``(-1, 1)``.
        nu: Volatility of volatility, non-negative.
        shift: Added to both forward and strike before the formulas are
            applied, so that a negative forward becomes a positive shifted
            one. Zero means unshifted.
    """

    alpha: float
    beta: float = DEFAULT_BETA
    rho: float = 0.0
    nu: float = 0.0
    shift: float = 0.0

    def __post_init__(self) -> None:
        if self.alpha <= 0.0:
            raise ValueError(f"alpha must be positive, got {self.alpha!r}")
        if not 0.0 <= self.beta <= 1.0:
            raise ValueError(f"beta must lie in [0, 1], got {self.beta!r}")
        if not -1.0 < self.rho < 1.0:
            raise ValueError(f"rho must lie strictly inside (-1, 1), got {self.rho!r}")
        if self.nu < 0.0:
            raise ValueError(f"nu must be non-negative, got {self.nu!r}")
        if self.shift < 0.0:
            raise ValueError(f"shift must be non-negative, got {self.shift!r}")

    def to_dict(self) -> dict[str, Any]:
        """Serialise the four parameters and the shift."""
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "rho": self.rho,
            "nu": self.nu,
            "shift": self.shift,
        }


def _shifted(parameters: SABRParameters, forward: float, strike: float) -> tuple[float, float]:
    """Apply the shift and refuse when the result is still not positive."""
    f, k = forward + parameters.shift, strike + parameters.shift
    if f <= 0.0 or k <= 0.0:
        raise ShiftRequiredError(
            f"SABR needs a positive shifted forward and strike; with shift "
            f"{parameters.shift!r} they are {f!r} and {k!r}. Hagan's expansion takes "
            "powers and logarithms of both. Increase the shift so that both clear zero."
        )
    return f, k


def _z_over_x(z: float, rho: float) -> float:
    """``z / x(z)`` from Hagan (2.17a), taken from its series near zero.

    The series is ``1 - rho z / 2 + O(z^2)``, and the minus sign is not
    obvious from the formula. Expanding
    ``x(z) = ln[(sqrt(1 - 2 rho z + z^2) + z - rho) / (1 - rho)]`` gives
    ``x(z) = z + rho z^2 / 2 + O(z^3)``, so ``z / x(z) = 1 / (1 + rho z / 2)``,
    which is ``1 - rho z / 2`` to first order. A plus here is wrong by
    ``rho z``, which at the threshold is parts in a hundred million — too
    small to move a price and large enough to make the two branches
    disagree, which is what ``test_the_series_branch_joins_the_ratio_branch``
    is there to catch.
    """
    if abs(z) < Z_SERIES_THRESHOLD:
        return 1.0 - 0.5 * rho * z
    numerator = math.sqrt(1.0 - 2.0 * rho * z + z * z) + z - rho
    return z / math.log(numerator / (1.0 - rho))


def _checked(value: float, flavour: str, strike: float, expiry: float) -> float:
    """Refuse a volatility the expansion has driven to or below zero.

    The ``O(nu^2 T)`` correction in Hagan's formulas is additive and can
    exceed one in magnitude. When it does, the product goes negative and the
    expansion has stopped approximating the model. Returning the number would
    push the failure downstream into a pricer, where it reads as a bad input
    rather than as a model boundary.
    """
    if value <= 0.0:
        raise ExpansionBreakdownError(
            f"Hagan's {flavour} expansion returns {value:.6e} at strike {strike!r} and "
            f"expiry {expiry!r} years. A volatility at or below zero means the "
            "O(nu^2 T) correction has overwhelmed the leading term and the expansion "
            "is outside its validity region — long expiry, high vol-of-vol, far wing. "
            "Shorten the expiry, lower nu, narrow the strike range, or use a model "
            "that solves SABR rather than expanding it."
        )
    return value


def lognormal_vol(
    parameters: SABRParameters, forward: float, strike: float, expiry: float
) -> float:
    """Black implied volatility from Hagan (2.17a), with (2.18) at the money.

    Args:
        parameters: The calibrated smile.
        forward: Forward rate as a decimal, before the shift.
        strike: Strike as a decimal, before the shift.
        expiry: Time to expiry in years, non-negative.

    Returns:
        The relative volatility as a decimal a year.

    Raises:
        ShiftRequiredError: The shifted forward or strike is not positive.
        ValueError: ``expiry`` is negative.
    """
    if expiry < 0.0:
        raise ValueError(f"expiry must be non-negative, got {expiry!r}")
    f, k = _shifted(parameters, forward, strike)
    alpha, beta, rho, nu = parameters.alpha, parameters.beta, parameters.rho, parameters.nu
    one_minus_beta = 1.0 - beta

    # The T-order correction, common to (2.17a) and (2.18).
    fk_pow = (f * k) ** one_minus_beta
    correction = 1.0 + (
        (one_minus_beta**2 / 24.0) * alpha * alpha / fk_pow
        + 0.25 * rho * beta * nu * alpha / math.sqrt(fk_pow)
        + ((2.0 - 3.0 * rho * rho) / 24.0) * nu * nu
    ) * expiry

    if f == k:
        return _checked(alpha / f**one_minus_beta * correction, "lognormal", strike, expiry)

    log_fk = math.log(f / k)
    denominator = (f * k) ** (one_minus_beta / 2.0) * (
        1.0
        + (one_minus_beta**2 / 24.0) * log_fk**2
        + (one_minus_beta**4 / 1920.0) * log_fk**4
    )
    z = (nu / alpha) * (f * k) ** (one_minus_beta / 2.0) * log_fk
    return _checked(
        alpha / denominator * _z_over_x(z, rho) * correction, "lognormal", strike, expiry
    )


def normal_vol(
    parameters: SABRParameters, forward: float, strike: float, expiry: float
) -> float:
    """Bachelier implied volatility from Hagan (A.59a), with its at-the-money limit.

    This is the one the swaption market quotes, so it is the one the cube
    stores. It is a separate expansion, not the lognormal one multiplied by
    the forward: that shortcut is the at-the-money leading term and it is
    wrong in the wings.

    Args:
        parameters: The calibrated smile.
        forward: Forward rate as a decimal, before the shift.
        strike: Strike as a decimal, before the shift.
        expiry: Time to expiry in years, non-negative.

    Returns:
        The absolute volatility as a decimal rate a year.

    Raises:
        ShiftRequiredError: The shifted forward or strike is not positive.
        ValueError: ``expiry`` is negative.
    """
    if expiry < 0.0:
        raise ValueError(f"expiry must be non-negative, got {expiry!r}")
    f, k = _shifted(parameters, forward, strike)
    alpha, beta, rho, nu = parameters.alpha, parameters.beta, parameters.rho, parameters.nu
    one_minus_beta = 1.0 - beta

    fk_pow = (f * k) ** one_minus_beta
    correction = 1.0 + (
        (-beta * (2.0 - beta) * alpha * alpha) / (24.0 * fk_pow)
        + (rho * alpha * nu * beta) / (4.0 * math.sqrt(fk_pow))
        + ((2.0 - 3.0 * rho * rho) / 24.0) * nu * nu
    ) * expiry

    if f == k:
        return _checked(alpha * f**beta * correction, "normal", strike, expiry)

    log_fk = math.log(f / k)
    numerator = 1.0 + (1.0 / 24.0) * log_fk**2 + (1.0 / 1920.0) * log_fk**4
    denominator = (
        1.0
        + (one_minus_beta**2 / 24.0) * log_fk**2
        + (one_minus_beta**4 / 1920.0) * log_fk**4
    )
    z = (nu / alpha) * (f * k) ** (one_minus_beta / 2.0) * log_fk
    return _checked(
        alpha
        * (f * k) ** (beta / 2.0)
        * (numerator / denominator)
        * _z_over_x(z, rho)
        * correction,
        "normal",
        strike,
        expiry,
    )


@dataclass(frozen=True)
class SABRCalibration(EngineResult):
    """A fitted smile and how well it fitted.

    Attributes:
        parameters: The calibrated :class:`SABRParameters`.
        strikes: Strikes the fit used, as decimals.
        quoted_vols: The normal volatilities quoted at those strikes.
        fitted_vols: What the calibrated smile says at the same strikes.
        rmse_bp: Root mean squared error in basis points of normal volatility.
        max_error_bp: Largest single deviation, in the same units.
        forward: The forward the smile is around.
        expiry: The expiry in years.
    """

    parameters: SABRParameters
    strikes: tuple[float, ...]
    quoted_vols: tuple[float, ...]
    fitted_vols: tuple[float, ...]
    rmse_bp: float
    max_error_bp: float
    forward: float
    expiry: float

    def payload_fields(self) -> dict[str, Any]:
        """The parameters, the quotes they were fitted to, and the errors."""
        return {
            "parameters": self.parameters.to_dict(),
            "forward": self.forward,
            "expiry": self.expiry,
            "strikes": list(self.strikes),
            "quoted_vols_bp": [v * 1e4 for v in self.quoted_vols],
            "fitted_vols_bp": [v * 1e4 for v in self.fitted_vols],
            "rmse_bp": self.rmse_bp,
            "max_error_bp": self.max_error_bp,
        }


def calibrate(
    forward: float,
    expiry: float,
    strikes: tuple[float, ...],
    normal_vols: tuple[float, ...],
    *,
    beta: float = DEFAULT_BETA,
    shift: float = 0.0,
    tolerance_bp: float | None = None,
) -> SABRCalibration:
    """Fit alpha, rho and nu to a smile of normal volatilities, with beta held fixed.

    Deterministic: the starting point is derived from the quotes rather than
    guessed, and ``scipy.optimize.least_squares`` runs with fixed tolerances,
    so the same smile gives the same parameters every time.

    Args:
        forward: Forward rate as a decimal.
        expiry: Time to expiry in years, strictly positive.
        strikes: Strikes as decimals; at least three, since three parameters
            are being fitted.
        normal_vols: Absolute volatilities as decimals, aligned with
            ``strikes``.
        beta: Backbone exponent, fixed. See the module docstring.
        shift: Shift applied to forward and strikes; required when the
            forward is at or below zero.
        tolerance_bp: When set, the fit is refused if the RMSE exceeds this
            many basis points. Left ``None``, the error is reported and the
            caller decides.

    Returns:
        The :class:`SABRCalibration`.

    Raises:
        CalibrationError: Fewer than three quotes, mismatched lengths, or an
            RMSE above ``tolerance_bp``. The message carries the error
            achieved, because a calibration that quietly returns its starting
            point is worse than one that refuses.
        ShiftRequiredError: The shifted forward is not positive.
        ValueError: ``expiry`` is not positive.
    """
    if expiry <= 0.0:
        raise ValueError(f"calibration needs a positive expiry, got {expiry!r}")
    if len(strikes) != len(normal_vols):
        raise CalibrationError(
            f"{len(strikes)} strikes and {len(normal_vols)} volatilities do not align"
        )
    if len(strikes) < 3:
        raise CalibrationError(
            f"fitting alpha, rho and nu needs at least three quotes, got {len(strikes)}. "
            "With fewer the system is underdetermined and any fit is an artefact of the "
            "starting point."
        )
    if forward + shift <= 0.0:
        raise ShiftRequiredError(
            f"forward {forward!r} with shift {shift!r} is not positive; SABR's expansion "
            "needs a positive shifted forward. Pass a larger shift."
        )

    # A starting point read off the data: at the money and with no vol-of-vol,
    # sigma_N ~ alpha F^beta, so alpha ~ sigma_atm / F^beta.
    atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - forward))
    alpha_start = normal_vols[atm_index] / (forward + shift) ** beta
    start = np.array([alpha_start, 0.0, 0.30])

    def residuals(x: np.ndarray) -> np.ndarray:
        candidate = SABRParameters(
            alpha=float(x[0]), beta=beta, rho=float(x[1]), nu=float(x[2]), shift=shift
        )
        out = []
        for k, v in zip(strikes, normal_vols, strict=True):
            try:
                out.append(normal_vol(candidate, forward, k, expiry) - v)
            except ExpansionBreakdownError:
                # Steer the optimiser out of the region rather than dying in
                # it. A large finite residual is a wall; a NaN is a cliff that
                # makes the whole step undefined.
                out.append(_BREAKDOWN_PENALTY)
        return np.array(out)

    solution = least_squares(
        residuals,
        start,
        bounds=(np.array([1e-10, -0.999, 0.0]), np.array([np.inf, 0.999, np.inf])),
        xtol=1e-14,
        ftol=1e-14,
        gtol=1e-14,
    )
    parameters = SABRParameters(
        alpha=float(solution.x[0]),
        beta=beta,
        rho=float(solution.x[1]),
        nu=float(solution.x[2]),
        shift=shift,
    )
    fitted = tuple(normal_vol(parameters, forward, k, expiry) for k in strikes)
    errors_bp = [(f - v) * 1e4 for f, v in zip(fitted, normal_vols, strict=True)]
    rmse_bp = math.sqrt(sum(e * e for e in errors_bp) / len(errors_bp))
    max_error_bp = max(abs(e) for e in errors_bp)

    if tolerance_bp is not None and rmse_bp > tolerance_bp:
        raise CalibrationError(
            f"SABR fitted the smile to {rmse_bp:.4f} bp RMSE, above the {tolerance_bp} bp "
            f"tolerance; worst strike off by {max_error_bp:.4f} bp. Parameters reached: "
            f"{parameters.to_dict()}"
        )

    evidence = Evidence(
        produced_by="volatility.sabr.calibrate",
        fields={
            "model": "sabr_hagan_2002",
            "equation": "A.59a (normal vol), 2.17a (lognormal vol)",
            "beta_fixed": beta,
            "beta_note": (
                "Fixed rather than fitted: on one smile beta and rho are close to "
                "unidentifiable, so a joint fit is ill-conditioned."
            ),
            "shift": shift,
            "quotes": len(strikes),
            "rmse_bp": rmse_bp,
            "max_error_bp": max_error_bp,
            "parameters": parameters.to_dict(),
            "optimizer": "scipy.least_squares",
            "optimizer_status": int(solution.status),
            "start": [float(v) for v in start],
            "approximation_note": (
                "Hagan's implied volatility is an asymptotic expansion with error "
                "O(nu^2 T), not a solution of the SABR model. It degrades with expiry "
                "and vol-of-vol, and it is not arbitrage-free in the low-strike wing. "
                "Call density_diagnostics to measure that on this smile."
            ),
        },
    )
    return SABRCalibration(
        evidence=evidence,
        parameters=parameters,
        strikes=tuple(strikes),
        quoted_vols=tuple(normal_vols),
        fitted_vols=fitted,
        rmse_bp=rmse_bp,
        max_error_bp=max_error_bp,
        forward=forward,
        expiry=expiry,
    )


def expansion_is_valid(
    parameters: SABRParameters, forward: float, strike: float, expiry: float
) -> bool:
    """Whether Hagan's expansion returns a usable volatility at this point.

    Args:
        parameters: The smile.
        forward: Forward rate as a decimal.
        strike: Strike as a decimal.
        expiry: Time to expiry in years.

    Returns:
        ``False`` when the expansion has left its validity region or the
        shifted forward or strike is not positive, ``True`` otherwise.
    """
    try:
        normal_vol(parameters, forward, strike, expiry)
    except (ExpansionBreakdownError, ShiftRequiredError):
        return False
    return True


def density_diagnostics(
    parameters: SABRParameters,
    forward: float,
    expiry: float,
    *,
    annuity: float = 1.0,
    low: float | None = None,
    high: float | None = None,
    points: int = 201,
) -> dict[str, Any]:
    """Measure whether the fitted smile implies a non-negative density.

    Breeden and Litzenberger: the risk-neutral density of the terminal rate is
    the second derivative of the call price with respect to strike. A smile
    that produces a negative one admits butterfly arbitrage. Hagan's expansion
    is known to do this in the low-strike wing at long expiries, so this is a
    measurement of a known limitation rather than a hunt for a bug.

    Args:
        parameters: The calibrated smile.
        forward: Forward rate as a decimal.
        expiry: Time to expiry in years.
        annuity: Numeraire for the prices; it cancels out of the verdict.
        low: Lowest strike to scan. Defaults to four normal standard
            deviations below the forward, floored just above the shift.
        high: Highest strike. Defaults to four standard deviations above.
        points: Strikes in the scan; more gives a finer but noisier second
            difference.

    Returns:
        A mapping with ``arbitrage_free`` (bool), the most negative density
        found, the strike where it occurred, and the scan's bounds.
    """
    from rates_engine.volatility import bachelier

    atm = normal_vol(parameters, forward, forward, expiry)
    width = 4.0 * atm * math.sqrt(expiry)
    lower = low if low is not None else max(forward - width, 1e-6 - parameters.shift + 1e-9)
    upper = high if high is not None else forward + width
    grid = [lower + (upper - lower) * i / (points - 1) for i in range(points)]
    prices = []
    for k in grid:
        try:
            prices.append(
                bachelier.price(
                    forward,
                    k,
                    expiry,
                    normal_vol(parameters, forward, k, expiry),
                    kind=OptionKind.CALL,
                    annuity=annuity,
                )
            )
        except ExpansionBreakdownError:
            # A negative implied volatility is a worse failure than a negative
            # density, and it is the answer to the question being asked.
            return {
                "arbitrage_free": False,
                "expansion_valid": False,
                "breakdown_strike": k,
                "min_density": None,
                "min_density_strike": None,
                "density_scale": None,
                "scan_low": lower,
                "scan_high": upper,
                "points": points,
                "method": "breeden_litzenberger_second_difference",
                "note": (
                    "Hagan's expansion returned a non-positive volatility inside the "
                    "scan, so no density exists to test. The smile is unusable here."
                ),
            }
    step = (upper - lower) / (points - 1)
    densities = [
        (prices[i - 1] - 2.0 * prices[i] + prices[i + 1]) / (step * step)
        for i in range(1, len(grid) - 1)
    ]
    worst = min(densities)
    worst_index = densities.index(worst)
    # Scale the tolerance by the density's own magnitude: a second difference
    # of small numbers carries rounding noise that is not an arbitrage.
    scale = max(max(densities), 1e-12)
    return {
        "arbitrage_free": bool(worst >= -1e-6 * scale),
        "expansion_valid": True,
        "breakdown_strike": None,
        "min_density": worst,
        "min_density_strike": grid[worst_index + 1],
        "density_scale": scale,
        "scan_low": lower,
        "scan_high": upper,
        "points": points,
        "method": "breeden_litzenberger_second_difference",
    }
