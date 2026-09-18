"""Four delta conventions, four different strikes, and no default.

"25 delta" does not name a strike. It names a strike *once you say which
delta you mean*, and FX has four in common use:

========================  ======================================
Convention                Call delta
========================  ======================================
spot, unadjusted          ``e^{-r_f T} N(d1)``
forward, unadjusted       ``N(d1)``
spot, premium-adjusted    ``e^{-r_f T} (K/F) N(d2)``
forward, premium-adjusted ``(K/F) N(d2)``
========================  ======================================

The spot-versus-forward part is a discounting choice. The premium
adjustment is not cosmetic: when the premium is paid in the base currency —
USD for USD/MXN, which is the market standard for that pair — buying the
option already gives you some of the delta you were hedging, and the delta
has to net it off. The two give *different strikes for the same quoted
delta*, by a margin that grows with volatility and expiry.

**So there is no default.** PRD-003's research gate could not establish
which convention USD/MXN trades on — Banxico, ISDA and CME are all
unreachable from the build environment — and a default here would be an
unverified convention silently determining every strike in the smile.
:class:`DeltaConvention` has no default value and
:func:`strike_from_delta` raises :class:`~rates_engine.errors.
DeltaConventionError` when asked to guess. That is AC-3.4, and the gap the
research left is the reason it earns its place rather than being
box-ticking.

**Premium-adjusted delta is not monotone in the strike.** For a call it
rises from zero, peaks, and falls back to zero, so most deltas have two
strikes. The market convention is the out-of-the-money branch, past the
peak, and that is what :func:`strike_from_delta` returns. A delta above the
peak has no strike at all and is refused rather than approximated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rates_engine.errors import DeltaConventionError
from rates_engine.fx.garman_kohlhagen import d1_d2, forward
from rates_engine.volatility import standard_normal_cdf
from rates_engine.volatility.kinds import OptionKind

__all__ = ["DeltaBasis", "PremiumAdjustment", "DeltaConvention", "delta", "strike_from_delta"]


class DeltaBasis(StrEnum):
    """Whether the delta is against spot or against the forward."""

    SPOT = "spot"
    FORWARD = "forward"


class PremiumAdjustment(StrEnum):
    """Whether the premium's own delta is netted off.

    ``UNADJUSTED``
        The premium is paid in the quote currency, so it carries no base
        currency exposure and nothing is netted.
    ``PREMIUM_ADJUSTED``
        The premium is paid in the base currency, so it is itself a
        position in the base currency and the delta nets it off.
    """

    UNADJUSTED = "unadjusted"
    PREMIUM_ADJUSTED = "premium_adjusted"


@dataclass(frozen=True)
class DeltaConvention:
    """One of the four, stated rather than assumed.

    Attributes:
        basis: Spot or forward.
        adjustment: Whether the premium's delta is netted off.
    """

    basis: DeltaBasis
    adjustment: PremiumAdjustment

    @property
    def is_premium_adjusted(self) -> bool:
        """True when the premium's own delta is netted off."""
        return self.adjustment is PremiumAdjustment.PREMIUM_ADJUSTED

    @property
    def name(self) -> str:
        """``"spot premium_adjusted"`` and the other three."""
        return f"{self.basis.value} {self.adjustment.value}"

    def to_dict(self) -> dict[str, Any]:
        """Serialise both halves; either one alone is ambiguous."""
        return {
            "delta_basis": self.basis.value,
            "premium_adjustment": self.adjustment.value,
            "delta_convention": self.name,
        }


def _require(convention: DeltaConvention | None) -> DeltaConvention:
    if convention is None:
        raise DeltaConventionError(
            "a delta does not name a strike until the convention is stated. FX uses "
            f"four: {', '.join(f'{b.value} {a.value}' for b in DeltaBasis for a in PremiumAdjustment)}. "
            "There is no default here because PRD-003's research gate could not "
            "establish which one USD/MXN trades on, and a default would let an "
            "unverified convention set every strike in the smile."
        )
    return convention


def delta(
    spot: float,
    strike: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind,
    convention: DeltaConvention | None = None,
) -> float:
    """The option's delta under a stated convention.

    Args:
        spot: Spot rate, quote per base.
        strike: Strike.
        expiry: Time to expiry in years.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal.
        kind: Call or put.
        convention: Which of the four. Required.

    Returns:
        The delta, positive for a call and negative for a put.

    Raises:
        DeltaConventionError: ``convention`` was not given.
        ValueError: Expiry or volatility is not positive.
    """
    rule = _require(convention)
    sign = kind.sign
    first, second = d1_d2(spot, strike, expiry, r_domestic, r_foreign, volatility)
    discount = math.exp(-r_foreign * expiry) if rule.basis is DeltaBasis.SPOT else 1.0
    if not rule.is_premium_adjusted:
        return sign * discount * standard_normal_cdf(sign * first)
    outright = forward(spot, expiry, r_domestic, r_foreign)
    return sign * discount * (strike / outright) * standard_normal_cdf(sign * second)


def _inverse_normal_cdf(probability: float) -> float:
    """The standard normal quantile.

    Uses ``scipy.special.ndtri``, imported here rather than at module level
    so that importing this package stays free of SciPy's start-up cost, in
    keeping with the rest of the engine.
    """
    from scipy.special import ndtri

    return float(ndtri(probability))


def _unadjusted_strike(
    spot: float,
    magnitude: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind,
    basis: DeltaBasis,
) -> float:
    """Closed form: the unadjusted conventions invert directly."""
    scaled = magnitude * (math.exp(r_foreign * expiry) if basis is DeltaBasis.SPOT else 1.0)
    if not 0.0 < scaled < 1.0:
        raise DeltaConventionError(
            f"a delta of {magnitude} is not attainable under the {basis.value} unadjusted "
            f"convention at expiry {expiry}: it implies N(d1) = {scaled:.6f}, outside (0, 1)"
        )
    first = kind.sign * _inverse_normal_cdf(scaled)
    root = volatility * math.sqrt(expiry)
    outright = forward(spot, expiry, r_domestic, r_foreign)
    return outright * math.exp(-first * root + 0.5 * root * root)


def _premium_adjusted_strike(
    spot: float,
    magnitude: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind,
    basis: DeltaBasis,
) -> float:
    """Numerical: ``K`` appears on both sides, and the map is not monotone.

    The premium-adjusted delta of a call rises from zero as the strike
    leaves zero, peaks, and falls back to zero. Two strikes give most
    deltas. The market means the out-of-the-money one, past the peak, where
    the delta is decreasing — so the peak is located first and the bisection
    runs on that side only.
    """
    convention = DeltaConvention(basis, PremiumAdjustment.PREMIUM_ADJUSTED)

    def at(strike: float) -> float:
        return abs(
            delta(spot, strike, expiry, r_domestic, r_foreign, volatility, kind, convention)
        )

    outright = forward(spot, expiry, r_domestic, r_foreign)
    # Locate the peak by golden-section search over a wide bracket in log
    # strike. Wide because a high-volatility, long-dated smile moves it a
    # long way from the forward.
    lo, hi = math.log(outright) - 6.0 * volatility * math.sqrt(expiry) - 2.0, math.log(
        outright
    ) + 6.0 * volatility * math.sqrt(expiry) + 2.0
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    left, right = hi - phi * (hi - lo), lo + phi * (hi - lo)
    f_left, f_right = at(math.exp(left)), at(math.exp(right))
    for _ in range(200):
        if f_left < f_right:
            lo, left, f_left = left, right, f_right
            right = lo + phi * (hi - lo)
            f_right = at(math.exp(right))
        else:
            hi, right, f_right = right, left, f_left
            left = hi - phi * (hi - lo)
            f_left = at(math.exp(left))
    peak_log = 0.5 * (lo + hi)
    peak = at(math.exp(peak_log))
    if magnitude > peak:
        raise DeltaConventionError(
            f"no strike has a {basis.value} premium-adjusted delta of {magnitude} here: the "
            f"largest attainable is {peak:.6f}, at strike {math.exp(peak_log):.6f}. "
            "Premium-adjusted delta is not monotone in the strike, so a delta above its "
            "peak names no strike at all rather than an extreme one."
        )

    # Bisect on the decreasing branch: strikes above the peak for a call,
    # below it for a put.
    if kind is OptionKind.CALL:
        low, high = math.exp(peak_log), math.exp(peak_log) + 20.0 * outright
        while at(high) > magnitude:
            high *= 2.0
        for _ in range(300):
            mid = 0.5 * (low + high)
            if at(mid) > magnitude:
                low = mid
            else:
                high = mid
        return 0.5 * (low + high)
    low, high = 1e-12, math.exp(peak_log)
    for _ in range(300):
        mid = 0.5 * (low + high)
        if at(mid) > magnitude:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)


def strike_from_delta(
    spot: float,
    target_delta: float,
    expiry: float,
    r_domestic: float,
    r_foreign: float,
    volatility: float,
    kind: OptionKind,
    convention: DeltaConvention | None = None,
) -> float:
    """The strike whose delta is ``target_delta`` under a stated convention.

    Args:
        spot: Spot rate.
        target_delta: The delta as a positive magnitude — ``0.25`` for both
            a 25-delta call and a 25-delta put. The sign comes from ``kind``,
            because "25 delta put" is universally said with a positive
            number.
        expiry: Time to expiry in years.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: Lognormal volatility as a decimal.
        kind: Call or put.
        convention: Which of the four. Required.

    Returns:
        The strike.

    Raises:
        DeltaConventionError: No convention was given, or the delta is not
            attainable under the one that was.
        ValueError: Expiry or volatility is not positive, or the delta is
            not a magnitude in ``(0, 1)``.
    """
    rule = _require(convention)
    if not 0.0 < target_delta < 1.0:
        raise ValueError(
            f"target_delta is a magnitude in (0, 1) — 0.25 for a 25-delta put as well "
            f"as a 25-delta call; got {target_delta!r}"
        )
    if expiry <= 0.0 or volatility <= 0.0:
        raise ValueError(
            f"a strike from a delta needs positive expiry and volatility; got "
            f"expiry={expiry!r}, volatility={volatility!r}"
        )
    if rule.is_premium_adjusted:
        return _premium_adjusted_strike(
            spot, target_delta, expiry, r_domestic, r_foreign, volatility, kind, rule.basis
        )
    return _unadjusted_strike(
        spot, target_delta, expiry, r_domestic, r_foreign, volatility, kind, rule.basis
    )
