"""Black: the rate is lognormal, which is elegant until the rate is not positive.

    d1 = [ln(F/K) + sigma^2 T / 2] / (sigma sqrt(T)),   d2 = d1 - sigma sqrt(T)
    V_call = A [ F N(d1) - K N(d2) ]
    V_put  = A [ K N(-d2) - F N(-d1) ]

Volatility is relative: ``sigma`` is a fraction a year, so 30% is ``0.30``.

**Where it refuses.** A non-positive forward has no logarithm, and neither
does a negative strike. Both raise
:class:`~rates_engine.errors.ShiftRequiredError` rather than returning
something: a lognormal model at a negative rate is not a hard case, it is the
wrong model, and the answer is a shift or Bachelier. A strike of exactly zero
is different — the limit exists and is finite, ``V_call = A F``, so it is
returned rather than refused. PRD-002 AC-2.2 is that limit.
"""

from __future__ import annotations

import math

from rates_engine.errors import ShiftRequiredError
from rates_engine.volatility._gaussian import standard_normal_cdf, standard_normal_pdf
from rates_engine.volatility.kinds import OptionKind

__all__ = ["price", "implied_lognormal_vol", "vega", "delta"]


def _check_positive(forward: float, strike: float) -> None:
    if forward <= 0.0:
        raise ShiftRequiredError(
            f"Black needs a positive forward and this one is {forward!r}. A lognormal rate "
            "cannot reach zero, so the model is not merely strained here, it is wrong. "
            "Use Bachelier, which the swaption market quotes in, or a shifted model."
        )
    if strike < 0.0:
        raise ShiftRequiredError(
            f"Black needs a non-negative strike and this one is {strike!r}. "
            "Use Bachelier or a shifted model."
        )


def price(
    forward: float,
    strike: float,
    expiry: float,
    sigma: float,
    *,
    kind: OptionKind = OptionKind.CALL,
    annuity: float = 1.0,
) -> float:
    """Black price of an option on a rate.

    Args:
        forward: Forward rate as a decimal, strictly positive.
        strike: Strike as a decimal, non-negative. Zero returns the limit.
        expiry: Time to expiry in years. Zero gives the intrinsic value.
        sigma: Relative volatility as a decimal a year; 30% is ``0.30``. Zero
            gives the intrinsic value.
        kind: Call or put on the rate.
        annuity: The numeraire the payoff is scaled by.

    Returns:
        The price in the same units as ``annuity``.

    Raises:
        ShiftRequiredError: The forward is not positive, or the strike is
            negative.
        ValueError: ``sigma`` or ``expiry`` is negative.
    """
    if sigma < 0.0:
        raise ValueError(f"sigma must be non-negative, got {sigma!r}")
    if expiry < 0.0:
        raise ValueError(f"expiry must be non-negative, got {expiry!r}")
    _check_positive(forward, strike)

    dispersion = sigma * math.sqrt(expiry)
    if dispersion == 0.0:
        return annuity * max(kind.sign * (forward - strike), 0.0)
    if strike == 0.0:
        # The limit as K -> 0: a call is certain to be exercised and is worth
        # the forward; a put is worthless. PRD-002 AC-2.2.
        return annuity * forward if kind is OptionKind.CALL else 0.0

    d1 = (math.log(forward / strike) + 0.5 * dispersion * dispersion) / dispersion
    d2 = d1 - dispersion
    sign = kind.sign
    return annuity * sign * (
        forward * standard_normal_cdf(sign * d1) - strike * standard_normal_cdf(sign * d2)
    )


def vega(
    forward: float, strike: float, expiry: float, sigma: float, *, annuity: float = 1.0
) -> float:
    """Sensitivity of the Black price to relative volatility, in price per unit sigma.

    Args:
        forward: Forward rate as a decimal, strictly positive.
        strike: Strike as a decimal, non-negative.
        expiry: Time to expiry in years.
        sigma: Relative volatility as a decimal a year.
        annuity: The numeraire.

    Returns:
        ``d(price) / d(sigma)``, the same for a call and a put.

    Raises:
        ShiftRequiredError: The forward is not positive or the strike negative.
    """
    _check_positive(forward, strike)
    dispersion = sigma * math.sqrt(expiry)
    if dispersion == 0.0 or strike == 0.0:
        return 0.0
    d1 = (math.log(forward / strike) + 0.5 * dispersion * dispersion) / dispersion
    return annuity * forward * math.sqrt(expiry) * standard_normal_pdf(d1)


def delta(
    forward: float,
    strike: float,
    expiry: float,
    sigma: float,
    *,
    kind: OptionKind = OptionKind.CALL,
    annuity: float = 1.0,
) -> float:
    """Sensitivity of the Black price to the forward rate.

    Args:
        forward: Forward rate as a decimal, strictly positive.
        strike: Strike as a decimal, non-negative.
        expiry: Time to expiry in years.
        sigma: Relative volatility as a decimal a year.
        kind: Call or put on the rate.
        annuity: The numeraire.

    Returns:
        ``d(price) / d(forward)``.

    Raises:
        ShiftRequiredError: The forward is not positive or the strike negative.
    """
    _check_positive(forward, strike)
    dispersion = sigma * math.sqrt(expiry)
    if dispersion == 0.0:
        intrinsic = kind.sign * (forward - strike)
        return annuity * kind.sign * (1.0 if intrinsic > 0.0 else 0.0)
    if strike == 0.0:
        return annuity if kind is OptionKind.CALL else 0.0
    d1 = (math.log(forward / strike) + 0.5 * dispersion * dispersion) / dispersion
    return annuity * kind.sign * standard_normal_cdf(kind.sign * d1)


def implied_lognormal_vol(
    target: float,
    forward: float,
    strike: float,
    expiry: float,
    *,
    kind: OptionKind = OptionKind.CALL,
    annuity: float = 1.0,
    tolerance: float = 1e-14,
    max_iterations: int = 100,
) -> float:
    """Invert :func:`price` for the relative volatility.

    Newton on vega inside a maintained bracket, as in
    :func:`rates_engine.volatility.bachelier.implied_normal_vol`.

    Args:
        target: The price to match, in the same units as ``annuity``.
        forward: Forward rate as a decimal, strictly positive.
        strike: Strike as a decimal, strictly positive — at a zero strike the
            price does not depend on volatility, so nothing can be inverted.
        expiry: Time to expiry in years, strictly positive.
        kind: Call or put on the rate.
        annuity: The numeraire.
        tolerance: Absolute price tolerance, relative to ``annuity``.
        max_iterations: Cap on the iteration count.

    Returns:
        The relative volatility as a decimal a year.

    Raises:
        ShiftRequiredError: The forward is not positive or the strike negative.
        ValueError: The expiry, annuity or strike is not positive, or the
            target is below the intrinsic value.
    """
    if expiry <= 0.0:
        raise ValueError(f"implied volatility needs a positive expiry, got {expiry!r}")
    if annuity <= 0.0:
        raise ValueError(f"implied volatility needs a positive annuity, got {annuity!r}")
    _check_positive(forward, strike)
    if strike == 0.0:
        raise ValueError(
            "at a zero strike the Black price is the forward whatever the volatility, "
            "so there is no volatility to invert"
        )
    intrinsic = annuity * max(kind.sign * (forward - strike), 0.0)
    scaled_tolerance = tolerance * annuity
    if target < intrinsic - scaled_tolerance:
        raise ValueError(
            f"price {target!r} is below the intrinsic value {intrinsic!r}; no volatility "
            "reproduces it"
        )
    if target <= intrinsic + scaled_tolerance:
        return 0.0

    low, high = 0.0, 0.10
    while price(forward, strike, expiry, high, kind=kind, annuity=annuity) < target:
        low, high = high, high * 2.0
        if high > 100.0:  # pragma: no cover - a 10,000% lognormal vol is not a market
            raise ValueError(f"price {target!r} is not attainable below a sigma of 100")

    sigma = 0.5 * (low + high)
    for _ in range(max_iterations):
        value = price(forward, strike, expiry, sigma, kind=kind, annuity=annuity)
        difference = value - target
        if abs(difference) <= scaled_tolerance:
            return sigma
        if difference > 0.0:
            high = sigma
        else:
            low = sigma
        slope = vega(forward, strike, expiry, sigma, annuity=annuity)
        step = sigma - difference / slope if slope > 0.0 else sigma
        sigma = step if low < step < high else 0.5 * (low + high)
    return sigma
