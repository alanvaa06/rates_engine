"""Bachelier: the rate is normal, so it may go negative and the maths does not care.

The model the swaption market quotes in. Its virtue is exactly the one Black
lacks: a rate that diffuses arithmetically has no trouble crossing zero, and
between 2014 and 2022 a great many of them did.

    V_call = A [ (F - K) N(d) + sigma sqrt(T) n(d) ],   d = (F - K) / (sigma sqrt(T))
    V_put  = A [ (K - F) N(-d) + sigma sqrt(T) n(d) ]

Volatility is absolute: ``sigma`` is a rate a year, so 100 bp is ``0.01``.
Put-call parity, ``V_call - V_put = A (F - K)``, holds identically rather than
approximately — it follows from ``N(d) + N(-d) = 1`` with no cancellation, so
the test of it in ``tests/test_swaptions.py`` is tight to machine precision.
"""

from __future__ import annotations

import math

from rates_engine.volatility._gaussian import standard_normal_cdf, standard_normal_pdf
from rates_engine.volatility.kinds import OptionKind

__all__ = ["price", "implied_normal_vol", "vega", "delta"]


def price(
    forward: float,
    strike: float,
    expiry: float,
    sigma: float,
    *,
    kind: OptionKind = OptionKind.CALL,
    annuity: float = 1.0,
) -> float:
    """Bachelier price of an option on a rate.

    Args:
        forward: Forward rate as a decimal. May be negative or zero.
        strike: Strike as a decimal. May be negative or zero.
        expiry: Time to expiry in years. Zero gives the intrinsic value.
        sigma: Absolute volatility as a decimal rate a year; 100 bp is
            ``0.01``. Zero gives the intrinsic value.
        kind: Call or put on the rate.
        annuity: The numeraire the payoff is scaled by — the swap annuity for
            a swaption, the discounted accrual for a caplet.

    Returns:
        The price in the same units as ``annuity``.

    Raises:
        ValueError: ``sigma`` or ``expiry`` is negative.
    """
    if sigma < 0.0:
        raise ValueError(f"sigma must be non-negative, got {sigma!r}")
    if expiry < 0.0:
        raise ValueError(f"expiry must be non-negative, got {expiry!r}")
    moneyness = kind.sign * (forward - strike)
    dispersion = sigma * math.sqrt(expiry)
    if dispersion == 0.0:
        return annuity * max(moneyness, 0.0)
    d = moneyness / dispersion
    return annuity * (moneyness * standard_normal_cdf(d) + dispersion * standard_normal_pdf(d))


def vega(
    forward: float, strike: float, expiry: float, sigma: float, *, annuity: float = 1.0
) -> float:
    """Sensitivity of the Bachelier price to absolute volatility, in price per unit sigma.

    The same for a call and a put, which is put-call parity differentiated:
    their difference does not depend on volatility.

    Args:
        forward: Forward rate as a decimal.
        strike: Strike as a decimal.
        expiry: Time to expiry in years.
        sigma: Absolute volatility as a decimal rate a year.
        annuity: The numeraire.

    Returns:
        ``d(price) / d(sigma)``. Divide by 1e4 for the change per basis point.
    """
    dispersion = sigma * math.sqrt(expiry)
    if dispersion == 0.0:
        return annuity * math.sqrt(expiry) * standard_normal_pdf(0.0)
    d = (forward - strike) / dispersion
    return annuity * math.sqrt(expiry) * standard_normal_pdf(d)


def delta(
    forward: float,
    strike: float,
    expiry: float,
    sigma: float,
    *,
    kind: OptionKind = OptionKind.CALL,
    annuity: float = 1.0,
) -> float:
    """Sensitivity of the Bachelier price to the forward rate.

    Args:
        forward: Forward rate as a decimal.
        strike: Strike as a decimal.
        expiry: Time to expiry in years.
        sigma: Absolute volatility as a decimal rate a year.
        kind: Call or put on the rate.
        annuity: The numeraire.

    Returns:
        ``d(price) / d(forward)``, which is ``annuity * N(d)`` for a call.
    """
    dispersion = sigma * math.sqrt(expiry)
    if dispersion == 0.0:
        intrinsic = kind.sign * (forward - strike)
        return annuity * kind.sign * (1.0 if intrinsic > 0.0 else 0.0)
    d = kind.sign * (forward - strike) / dispersion
    return annuity * kind.sign * standard_normal_cdf(d)


def implied_normal_vol(
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
    """Invert :func:`price` for the absolute volatility.

    Newton on vega with a bisection fallback. Vega is strictly positive for
    any positive dispersion and the price is strictly increasing in sigma, so
    the root is unique and the bracket is honest; the fallback exists for the
    deep wings where Newton's first step can overshoot into negative sigma.

    Args:
        target: The price to match, in the same units as ``annuity``.
        forward: Forward rate as a decimal.
        strike: Strike as a decimal.
        expiry: Time to expiry in years, strictly positive.
        kind: Call or put on the rate.
        annuity: The numeraire.
        tolerance: Absolute price tolerance, relative to ``annuity``.
        max_iterations: Cap on the iteration count.

    Returns:
        The absolute volatility as a decimal rate a year.

    Raises:
        ValueError: ``expiry`` is not positive, ``annuity`` is not positive,
            or the target is below the intrinsic value, where no volatility
            reproduces it.
    """
    if expiry <= 0.0:
        raise ValueError(f"implied volatility needs a positive expiry, got {expiry!r}")
    if annuity <= 0.0:
        raise ValueError(f"implied volatility needs a positive annuity, got {annuity!r}")
    intrinsic = annuity * max(kind.sign * (forward - strike), 0.0)
    scaled_tolerance = tolerance * annuity
    if target < intrinsic - scaled_tolerance:
        raise ValueError(
            f"price {target!r} is below the intrinsic value {intrinsic!r}; no volatility "
            "reproduces it, and a negative one is not an answer"
        )
    if target <= intrinsic + scaled_tolerance:
        return 0.0

    low, high = 0.0, 0.01
    while price(forward, strike, expiry, high, kind=kind, annuity=annuity) < target:
        low, high = high, high * 2.0
        if high > 100.0:  # pragma: no cover - a 10,000% normal vol is not a market
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
