"""Garman-Kohlhagen: Black-Scholes with two interest rates.

An FX option's underlying pays a continuous yield — the foreign interest
rate — so the spot is discounted by ``e^{-r_f T}`` where an equity would
carry a dividend yield. That is the whole of the difference from Black-
Scholes, and it is why the formula is one function rather than a model.

**Which rate is which.** ``r_domestic`` belongs to the quote currency, the
one the price is expressed in; ``r_foreign`` to the base currency, the one
you buy a unit of. For USD/MXN that is MXN domestic and USD foreign.
Reversing them produces a well-formed price that is wrong by the interest
differential, which for this pair is several hundred basis points, so both
are named rather than positional-by-habit.

**No volatility type here.** Unlike the rate options in
:mod:`rates_engine.volatility`, an FX volatility is unambiguously lognormal
— the market quotes it in percent and there is no normal-vol convention to
confuse it with. The ambiguity in FX is the *delta* convention, which is
:mod:`rates_engine.fx.delta`'s problem, and it is a real one.
"""

from __future__ import annotations

import math

from rates_engine.volatility import standard_normal_cdf, standard_normal_pdf
from rates_engine.volatility.kinds import OptionKind

__all__ = [
    "forward",
    "d1_d2",
    "price",
    "delta_spot",
    "vega",
    "gamma",
    "vanna",
    "volga",
    "theta",
]


def forward(spot: float, expiry: float, r_domestic: float, r_foreign: float) -> float:
    """The outright forward implied by covered interest parity.

    Args:
        spot: Spot rate, quote currency per unit of base.
        expiry: Time to delivery in years.
        r_domestic: Continuously compounded rate of the quote currency.
        r_foreign: Continuously compounded rate of the base currency.

    Returns:
        The forward rate, in the same units as ``spot``.
    """
    return spot * math.exp((r_domestic - r_foreign) * expiry)


def d1_d2(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
) -> tuple[float, float]:
    """The two Black-Scholes arguments.

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years, positive.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal, positive.

    Returns:
        ``(d1, d2)``.

    Raises:
        ValueError: Any of spot, strike, expiry or volatility is not
            positive. The formula has no limit to take at those points that
            this function could return; the limits are handled in
            :func:`price`, where they are payoffs rather than arguments.
    """
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError(
            f"Garman-Kohlhagen is lognormal, so spot and strike must be positive; "
            f"got spot={spot!r}, strike={strike!r}"
        )
    if expiry <= 0.0 or volatility <= 0.0:
        raise ValueError(
            f"d1 and d2 are undefined at expiry={expiry!r}, volatility={volatility!r}; "
            "price() handles those as limits"
        )
    root = volatility * math.sqrt(expiry)
    first = (
        math.log(spot / strike) + (r_domestic - r_foreign + 0.5 * volatility * volatility) * expiry
    ) / root
    return first, first - root


def price(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind = OptionKind.CALL,
) -> float:
    """Value of a vanilla FX option, in quote currency per unit of base.

    Args:
        spot: Spot rate.
        strike: Strike.
        expiry: Time to expiry in years. Zero gives the intrinsic value.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives the
            discounted intrinsic value on the forward, which is the correct
            limit rather than a special case.
        kind: Call or put on the base currency.

    Returns:
        The premium, in quote currency per one unit of base.
    """
    sign = kind.sign
    if expiry <= 0.0:
        return max(sign * (spot - strike), 0.0)
    if volatility <= 0.0:
        outright = forward(spot, expiry, r_domestic, r_foreign)
        return math.exp(-r_domestic * expiry) * max(sign * (outright - strike), 0.0)
    first, second = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    return sign * (
        spot * math.exp(-r_foreign * expiry) * standard_normal_cdf(sign * first)
        - strike * math.exp(-r_domestic * expiry) * standard_normal_cdf(sign * second)
    )


def delta_spot(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind = OptionKind.CALL,
) -> float:
    """Unadjusted spot delta, ``e^{-r_f T} N(d1)`` for a call.

    The plainest of the four conventions, here because the greeks below are
    written against it. :mod:`rates_engine.fx.delta` has the other three and
    the refusal that stops one being assumed.
    """
    if expiry <= 0.0 or volatility <= 0.0:
        intrinsic = 1.0 if kind.sign * (spot - strike) > 0.0 else 0.0
        return kind.sign * intrinsic
    first, _ = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    return kind.sign * math.exp(-r_foreign * expiry) * standard_normal_cdf(kind.sign * first)


def vega(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
) -> float:
    """Sensitivity to volatility, per unit of volatility. Same for call and put."""
    if expiry <= 0.0 or volatility <= 0.0:
        return 0.0
    first, _ = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    return spot * math.exp(-r_foreign * expiry) * standard_normal_pdf(first) * math.sqrt(expiry)


def gamma(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
) -> float:
    """Second derivative in spot. Same for call and put."""
    if expiry <= 0.0 or volatility <= 0.0:
        return 0.0
    first, _ = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    return (
        math.exp(-r_foreign * expiry)
        * standard_normal_pdf(first)
        / (spot * volatility * math.sqrt(expiry))
    )


def vanna(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
) -> float:
    """Cross derivative in spot and volatility, ``-e^{-r_f T} phi(d1) d2 / sigma``.

    One of the two greeks the vanna-volga construction hedges. Same for call
    and put, which is what makes a risk reversal a pure vanna trade.
    """
    if expiry <= 0.0 or volatility <= 0.0:
        return 0.0
    first, second = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    return -math.exp(-r_foreign * expiry) * standard_normal_pdf(first) * second / volatility


def volga(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
) -> float:
    """Second derivative in volatility, ``vega d1 d2 / sigma``.

    The other greek vanna-volga hedges, and what a butterfly is a trade in.
    """
    if expiry <= 0.0 or volatility <= 0.0:
        return 0.0
    first, second = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    return vega(spot, strike, expiry, r_domestic, r_foreign, volatility) * first * second / volatility


def theta(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind = OptionKind.CALL,
) -> float:
    """Time decay per year, the analytic derivative rather than a bump.

    Negative for a long option in the ordinary case, and not always so: a
    deep in-the-money put on a high-rate quote currency can decay upwards,
    which is why this is not asserted to have a sign.
    """
    if expiry <= 0.0 or volatility <= 0.0:
        return 0.0
    sign = kind.sign
    first, second = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    carry = (
        -spot * math.exp(-r_foreign * expiry) * standard_normal_pdf(first) * volatility
        / (2.0 * math.sqrt(expiry))
    )
    foreign = (
        sign * r_foreign * spot * math.exp(-r_foreign * expiry)
        * standard_normal_cdf(sign * first)
    )
    domestic = (
        -sign * r_domestic * strike * math.exp(-r_domestic * expiry)
        * standard_normal_cdf(sign * second)
    )
    return carry + foreign + domestic
