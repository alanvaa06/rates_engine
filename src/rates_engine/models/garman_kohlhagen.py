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

**Zero is a limit; below zero is an error.** At zero expiry the option is
its payoff and at zero volatility it is the discounted payoff on the
forward, so every function here takes those values and returns the limit.
A negative expiry or volatility is not a limit of anything: ``price`` used
to read a volatility of -0.115 as zero and an expiry three months gone as
the intrinsic value today, so both now raise, as does a NaN.

**No volatility type here.** Unlike the rate options in
:mod:`rates_engine.volatility`, an FX volatility is unambiguously lognormal
— the market quotes it in percent and there is no normal-vol convention to
confuse it with. The ambiguity in FX is the *delta* convention, which is
:mod:`rates_engine.models.fx_delta`'s problem, and it is a real one.
"""

from __future__ import annotations

import math

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.models.gaussian import standard_normal_cdf, standard_normal_pdf

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


def _require_domain(expiry: float, volatility: float) -> None:
    """Refuse a negative or non-finite expiry or volatility; zero is a limit."""
    if not 0.0 <= expiry < math.inf:
        raise ValueError(
            f"expiry is a time to expiry in years, zero or more; got {expiry!r}. A "
            "negative one is an option that has already expired, whose value is not "
            "its intrinsic value today."
        )
    if not 0.0 <= volatility < math.inf:
        raise ValueError(
            f"volatility is a lognormal volatility as a decimal, zero or more; got "
            f"{volatility!r}. A negative volatility is not a limit of the formula, and "
            "reading it as zero prices the option as if the rate could not move."
        )


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
    _require_domain(expiry, volatility)
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

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
            Zero is the limit described above; below zero is not one.
    """
    _require_domain(expiry, volatility)
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
    written against it. :mod:`rates_engine.models.fx_delta` has the other three and
    the refusal that stops one being assumed.

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years. Zero or less gives the intrinsic
            delta, 0 or the sign of the option, which is the limit rather
            than a guard.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives the
            deterministic limit, which is not the zero-expiry one: the
            option finishes in the money exactly when the *forward* is, and
            its delta is then the discounted forward's, ``e^{-r_f T}`` —
            half that when forward and strike coincide, where ``N(d1)``
            tends to one half.
        kind: Call or put on the base currency.

    Returns:
        Delta as a decimal, signed by ``kind``: in ``[0, e^{-r_f T}]`` for a
        call and ``[-e^{-r_f T}, 0]`` for a put. The discount factor, not
        one, is the bound — which is why a "90 delta" spot option can be
        unattainable.

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
    """
    _require_domain(expiry, volatility)
    if expiry <= 0.0:
        intrinsic = 1.0 if kind.sign * (spot - strike) > 0.0 else 0.0
        return kind.sign * intrinsic
    if volatility <= 0.0:
        # Zero volatility used to share the zero-expiry branch: spot against
        # strike at magnitude one. But with time left the price is
        # e^{-r_d T} (F - K)+ (see price), whose slope in the spot is
        # e^{-r_d T} F / S = e^{-r_f T}, and the moneyness that matters is
        # the forward's. QuantLib's BlackCalculator at zero deviation agrees.
        outright = forward(spot, expiry, r_domestic, r_foreign)
        if outright == strike:
            probability = 0.5
        else:
            probability = 1.0 if kind.sign * (outright - strike) > 0.0 else 0.0
        return kind.sign * math.exp(-r_foreign * expiry) * probability
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
    """Sensitivity to volatility, per unit of volatility. Same for call and put.

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years. Zero or less gives zero: an expired
            option has no volatility exposure left.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives zero.

    Returns:
        Quote currency per unit of base per **one unit** of volatility, not
        per volatility point. Divide by 100 for the per-point figure a
        trader quotes.

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
    """
    _require_domain(expiry, volatility)
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
    """Second derivative in spot. Same for call and put.

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years. Zero or less gives zero rather than
            the divergent limit at the strike.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives zero, for
            the same reason.

    Returns:
        Change in delta per unit change in spot, per unit of base.

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
    """
    _require_domain(expiry, volatility)
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

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years. Zero or less gives zero.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives zero.

    Returns:
        Change in delta per **one unit** of volatility, equivalently change
        in vega per unit of spot. Zero at the strike where ``d2`` is, which
        is near but not at the at-the-money.

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
    """
    _require_domain(expiry, volatility)
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

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years. Zero or less gives zero.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives zero.

    Returns:
        Change in vega per **one unit** of volatility. Zero at the two
        strikes where ``d1`` or ``d2`` is, and positive in both wings, which
        is why a butterfly is long it.

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
    """
    _require_domain(expiry, volatility)
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

    Args:
        spot: Spot rate.
        strike: Strike, in the same units.
        expiry: Time to expiry in years. Zero or less gives zero: there is
            no time left to decay.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal. Zero gives zero.
        kind: Call or put on the base currency.

    Returns:
        Quote currency per unit of base **per year**. Divide by 365 for the
        per-day figure, which is how it is usually read.

    Raises:
        ValueError: ``expiry`` or ``volatility`` is negative or not finite.
    """
    _require_domain(expiry, volatility)
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
