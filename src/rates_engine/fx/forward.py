"""The FX forward: covered interest parity, and the part that is not.

Covered interest parity says the forward is pinned by the two discount
curves and nothing else:

``F = S * P_foreign(T) / P_domestic(T)``

It is an arbitrage relation, not a model, and until 2007 it held to within
transaction costs. It has not since. The residual — the cross-currency
basis — is a real, persistent, signed quantity that reflects the cost of
raising one currency against another in the swap market, and pretending it
is zero misprices every long-dated forward.

So this module returns both halves and never just their sum. Every result
carries the covered-interest-parity forward, the basis contribution, and
the outright; a caller who wants to know how much of a hedge's cost is
arbitrage-free carry and how much is funding can read it off rather than
recompute it.

**Which leg, and therefore which sign.** A cross-currency basis is quoted
as a spread on the **non-USD** leg, which for USD/MXN is the quote
currency, MXN. So the effective domestic rate is ``r_d + b`` and

``outright = cip_forward * exp(+b T)``

A negative basis — the persistent sign for most currencies against the
dollar — therefore *lowers* the outright: it is the premium the market
charges to obtain dollars, paid by accepting less on the peso leg.

This is worth stating at length because it is a sign, and a sign here is
the difference between a hedge and its opposite. An earlier version of this
module applied the spread to the base leg, ``exp(-b T)``, while its
docstring described the quote-leg convention — 976 pips apart at one year
on a 25 bp basis, with the prose and the formula each documenting the other
as wrong. The leg is now named in the code, in the evidence and in the
tests, and :data:`BASIS_LEG` exists so that a reader does not have to infer
it from an exponent.

The convention itself is **not verified from a primary source**: BIS, ISDA
and Banxico are all unreachable from this build environment, which is the
same gap `docs/forge/research/003-mxn-conventions.md` records for TIIE. It
is the standard one, stated explicitly so that a caller who knows better
can negate their input rather than discover the disagreement in a P&L.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_engine.curves.discount import DiscountCurve
from rates_engine.errors import (
    CurrencyMismatchError,
    CurveMismatchError,
    ImplausibleInputError,
)
from rates_engine.evidence import Evidence
from rates_engine.fx.quote import CurrencyPair
from rates_engine.results import EngineResult

__all__ = [
    "BASIS_LEG",
    "FXForwardResult",
    "forward_from_curves",
    "implied_basis",
    "MAX_PLAUSIBLE_BASIS_BP",
    "CIP_BROKEN_NOTE",
]

BASIS_LEG = "quote"
"""Which leg the cross-currency basis is a spread on.

``"quote"`` — the non-USD leg, MXN for USD/MXN — which is the market
convention. Named rather than implied, because the alternative reading
flips the sign of every basis-adjusted forward.
"""

MAX_PLAUSIBLE_BASIS_BP = 500.0
"""Beyond this, in basis points, the input is refused rather than used.

Five hundred basis points of cross-currency basis is a currency crisis, not
a quote. The number is a plausibility band and not a market observation —
the research gate could not reach a basis time series — so it is named,
documented and adjustable rather than buried in a comparison.
"""

CIP_BROKEN_NOTE = (
    "Covered interest parity has not held since 2007. The residual is the "
    "cross-currency basis: a persistent, signed spread reflecting the cost of "
    "raising one currency against another in the swap market. A forward computed "
    "from two curves alone is the parity forward, not the market's."
)


@dataclass(frozen=True)
class FXForwardResult(EngineResult):
    """A forward, split into the part parity explains and the part it does not.

    Attributes:
        outright: The forward rate, quote currency per unit of base.
        cip_forward: What covered interest parity alone implies.
        basis_component: ``outright - cip_forward``, in rate terms.
        basis_bp: The basis that produced it, in basis points.
        forward_points: ``outright - spot``, in pips of the pair.
        cip_points: The parity part of those points, in pips.
        basis_points_contribution: The basis part, in pips.
        spot: The spot rate used.
        pair: The pair, carrying the pip the points are counted in.
        delivery: The delivery date.
    """

    outright: float
    cip_forward: float
    basis_component: float
    basis_bp: float
    forward_points: float
    cip_points: float
    basis_points_contribution: float
    spot: float
    pair: CurrencyPair
    delivery: date

    def payload_fields(self) -> dict[str, Any]:
        """Both halves, in rate terms and in pips, and never only the sum."""
        return {
            "outright": self.outright,
            "cip_forward": self.cip_forward,
            "basis_component": self.basis_component,
            "basis_bp": self.basis_bp,
            "forward_points": self.forward_points,
            "cip_points": self.cip_points,
            "basis_points_contribution": self.basis_points_contribution,
            "basis_leg": BASIS_LEG,
            "spot": self.spot,
            "delivery": self.delivery.isoformat(),
            **self.pair.to_dict(),
            "cip_note": CIP_BROKEN_NOTE,
        }


def _check(pair: CurrencyPair, domestic: DiscountCurve, foreign: DiscountCurve) -> None:
    if foreign.currency is not pair.base:
        raise CurrencyMismatchError(
            f"{pair.name} has {pair.base.value} as its base currency, so the foreign "
            f"curve must be in {pair.base.value}; got {foreign.currency.value}. Getting "
            "the two curves the wrong way round inverts the forward points."
        )
    if domestic.currency is not pair.quote:
        raise CurrencyMismatchError(
            f"{pair.name} is quoted in {pair.quote.value}, so the domestic curve must be "
            f"in {pair.quote.value}; got {domestic.currency.value}."
        )
    if domestic.as_of != foreign.as_of:
        # Each curve discounts from its own valuation date, so two curves
        # struck on different days combine into a forward that is part
        # forward and part stale — six months apart is four thousand pips
        # on this pair, with the evidence reporting one year fraction for
        # both. This is the same class of error the currency check catches.
        raise CurveMismatchError(
            f"the {pair.quote.value} curve is as of {domestic.as_of} and the "
            f"{pair.base.value} curve as of {foreign.as_of}. A forward built from two "
            "valuation dates is part forward and part stale; roll one curve to the "
            "other's date first."
        )


def _plausible(basis_bp: float) -> None:
    if not math.isfinite(basis_bp) or abs(basis_bp) > MAX_PLAUSIBLE_BASIS_BP:
        raise ImplausibleInputError(
            f"a cross-currency basis of {basis_bp:.1f} bp is outside the "
            f"±{MAX_PLAUSIBLE_BASIS_BP:.0f} bp band this build will accept. That is a "
            "currency crisis rather than a quote, and the band is a plausibility "
            "check rather than a measurement — no basis time series was reachable "
            "when it was set. Widen MAX_PLAUSIBLE_BASIS_BP deliberately if you mean it."
        )


def forward_from_curves(
    pair: CurrencyPair,
    spot: float,
    delivery: date,
    domestic: DiscountCurve,
    foreign: DiscountCurve,
    *,
    basis_bp: float = 0.0,
    source_evidence: tuple[Evidence, ...] = (),
) -> FXForwardResult:
    """The outright forward, with the basis kept separate from the parity part.

    Args:
        pair: The currency pair, base first.
        spot: Spot rate, quote currency per unit of base.
        delivery: Delivery date, after both curves' valuation date.
        domestic: Discount curve of the quote currency.
        foreign: Discount curve of the base currency.
        basis_bp: Cross-currency basis in basis points, a spread on the
            quote currency leg (see :data:`BASIS_LEG`). Negative — the usual
            sign against the dollar — lowers the outright. Zero means the
            parity forward, and the result says so rather than implying
            parity holds.
        source_evidence: Evidence of the curves, chained in.

    Returns:
        The :class:`FXForwardResult`.

    Raises:
        CurrencyMismatchError: Either curve is not in the currency the pair
            says it should be.
        ImplausibleInputError: ``basis_bp`` is outside the plausibility band.
        ValueError: ``spot`` is not positive, or delivery is not after the
            valuation date.
    """
    _check(pair, domestic, foreign)
    _plausible(basis_bp)
    if spot <= 0.0:
        raise ValueError(f"a spot rate is positive; got {spot!r}")
    if delivery <= domestic.as_of:
        raise ValueError(
            f"delivery {delivery} is not after the valuation date {domestic.as_of}"
        )

    domestic_df = domestic.df(delivery)
    foreign_df = foreign.df(delivery)
    parity = spot * foreign_df / domestic_df

    # A spread on the quote (non-USD) leg: the effective domestic rate is
    # r_d + b, so the forward carries exp(+b T) over the same year fraction
    # the curves use. See BASIS_LEG.
    years = domestic.time(delivery)
    outright = parity * math.exp(basis_bp * 1e-4 * years)

    evidence = Evidence(
        produced_by="fx.forward_from_curves",
        fields={
            "pair": pair.name,
            "spot": spot,
            "delivery": delivery.isoformat(),
            "domestic_currency": domestic.currency.value,
            "foreign_currency": foreign.currency.value,
            "domestic_df": domestic_df,
            "foreign_df": foreign_df,
            "year_fraction": years,
            "cip_forward": parity,
            "basis_bp": basis_bp,
            "basis_leg": BASIS_LEG,
            "basis_component": outright - parity,
            "outright": outright,
            "cip_note": CIP_BROKEN_NOTE,
        },
        sources=source_evidence,
    )
    return FXForwardResult(
        evidence=evidence,
        outright=outright,
        cip_forward=parity,
        basis_component=outright - parity,
        basis_bp=basis_bp,
        forward_points=pair.pips(outright - spot),
        cip_points=pair.pips(parity - spot),
        basis_points_contribution=pair.pips(outright - parity),
        spot=spot,
        pair=pair,
        delivery=delivery,
    )


def implied_basis(
    pair: CurrencyPair,
    spot: float,
    market_forward: float,
    delivery: date,
    domestic: DiscountCurve,
    foreign: DiscountCurve,
    *,
    source_evidence: tuple[Evidence, ...] = (),
) -> FXForwardResult:
    """Back out the basis a quoted forward implies, given the two curves.

    The inverse of :func:`forward_from_curves`, and the honest way to read a
    market forward: whatever parity does not explain is the basis, and
    saying so is better than adjusting a curve until the forward comes out
    right.

    Args:
        pair: The currency pair.
        spot: Spot rate.
        market_forward: The quoted outright.
        delivery: Delivery date.
        domestic: Quote currency curve.
        foreign: Base currency curve.
        source_evidence: Evidence of the curves.

    Returns:
        An :class:`FXForwardResult` whose ``outright`` is the quote and
        whose ``basis_bp`` is what it implies.

    Raises:
        CurrencyMismatchError: A curve is in the wrong currency.
        ImplausibleInputError: The implied basis is outside the band, which
            usually means a curve or the quote is wrong rather than that the
            market has moved that far.
        ValueError: A rate is not positive, or delivery is not in the future.
    """
    _check(pair, domestic, foreign)
    if spot <= 0.0 or market_forward <= 0.0:
        raise ValueError(
            f"spot and forward are positive rates; got {spot!r} and {market_forward!r}"
        )
    if delivery <= domestic.as_of:
        raise ValueError(
            f"delivery {delivery} is not after the valuation date {domestic.as_of}"
        )

    parity = spot * foreign.df(delivery) / domestic.df(delivery)
    years = domestic.time(delivery)
    basis_bp = math.log(market_forward / parity) / years * 1e4
    _plausible(basis_bp)

    evidence = Evidence(
        produced_by="fx.implied_basis",
        fields={
            "pair": pair.name,
            "spot": spot,
            "market_forward": market_forward,
            "delivery": delivery.isoformat(),
            "cip_forward": parity,
            "implied_basis_bp": basis_bp,
            "basis_leg": BASIS_LEG,
            "year_fraction": years,
            "domestic_currency": domestic.currency.value,
            "foreign_currency": foreign.currency.value,
            "cip_note": CIP_BROKEN_NOTE,
            "note": (
                "The basis is what parity does not explain, read off rather than "
                "fitted. Adjusting a curve until the forward came out right would "
                "hide the same number inside the curve."
            ),
        },
        sources=source_evidence,
    )
    return FXForwardResult(
        evidence=evidence,
        outright=market_forward,
        cip_forward=parity,
        basis_component=market_forward - parity,
        basis_bp=basis_bp,
        forward_points=pair.pips(market_forward - spot),
        cip_points=pair.pips(parity - spot),
        basis_points_contribution=pair.pips(market_forward - parity),
        spot=spot,
        pair=pair,
        delivery=delivery,
    )
