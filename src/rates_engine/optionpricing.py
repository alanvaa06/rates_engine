"""Pricing the option instruments, and the model choice that has to be declared.

Kept apart from :mod:`rates_engine.pricing` because the two answer different
questions. A linear instrument has cashflows and a present value; an option
has a model, a volatility and a set of conventions about what that volatility
means, and every one of those has to reach the evidence. Folding them
together would give ``pv()`` a volatility argument that is meaningless for
three quarters of its callers.

**The model is never inferred.** Black and Bachelier take volatilities that
are not convertible into one another, so
:class:`~rates_engine.volatility.units.Volatility` already refuses to hand one
to the other. This module completes the loop: the model follows from the
units of the quote, so passing a normal volatility prices under Bachelier and
a lognormal one under Black, and there is no third possibility to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_engine.curves.discount import CurveSet
from rates_engine.evidence import Evidence
from rates_engine.instruments.capfloor import CapFloor, Caplet
from rates_engine.instruments.swaption import Swaption
from rates_engine.results import EngineResult
from rates_engine.volatility import bachelier, black
from rates_engine.volatility.units import Volatility

__all__ = [
    "OptionPriceResult",
    "model_for",
    "forward_swap_rate",
    "swaption_annuity",
    "swaption_pv",
    "cap_floor_pv",
    "caplet_pv",
]


def forward_swap_rate(swaption: Swaption, curve_set: CurveSet) -> float:
    """The forward swap rate a swaption is on, as a decimal.

    Lives here rather than on the contract because it is a pricing fact: it
    is the par rate of the underlying, and it is a martingale under the
    annuity measure, which is what makes a one-dimensional option formula
    correct for a swaption.

    Args:
        swaption: The contract.
        curve_set: Discount and projection curves.

    Returns:
        The par rate of the underlying swap.
    """
    from rates_engine.pricing import par_rate

    return par_rate(swaption.underlying, curve_set).value


def swaption_annuity(swaption: Swaption, curve_set: CurveSet) -> float:
    """The numeraire: discounted fixed-leg accrual times notional, in USD.

    Args:
        swaption: The contract.
        curve_set: Discount and projection curves.

    Returns:
        The annuity in USD per unit of rate.
    """
    from rates_engine.pricing import annuity as leg_annuity

    return leg_annuity(swaption.underlying, curve_set).value * swaption.notional


def model_for(volatility: Volatility) -> str:
    """Which model the units of a quote imply.

    Args:
        volatility: The quote.

    Returns:
        ``"bachelier"`` for an absolute volatility, ``"black"`` for a
        relative one. There is no default and no override: the two models
        take different numbers, and choosing the model separately from the
        quote is how a normal vol ends up in a lognormal formula.
    """
    return "bachelier" if volatility.is_normal else "black"


def _price(
    volatility: Volatility,
    forward: float,
    strike: float,
    expiry: float,
    kind: Any,
    numeraire: float,
) -> float:
    if volatility.is_normal:
        return bachelier.price(
            forward,
            strike,
            expiry,
            volatility.as_normal_decimal(),
            kind=kind,
            annuity=numeraire,
        )
    return black.price(
        forward,
        strike,
        expiry,
        volatility.as_lognormal_decimal(),
        kind=kind,
        annuity=numeraire,
    )


@dataclass(frozen=True)
class OptionPriceResult(EngineResult):
    """An option price and everything the model needed to produce it.

    Attributes:
        value: Present value in USD.
        model: ``"bachelier"`` or ``"black"``.
        volatility: The quote used, carrying its units.
        forward: The forward rate the option is on, as a decimal.
        strike: The strike as a decimal.
        expiry: Time to expiry in years.
        numeraire: The annuity or discounted accrual the payoff scaled by.
    """

    value: float
    model: str
    volatility: Volatility
    forward: float
    strike: float
    expiry: float
    numeraire: float

    def payload_fields(self) -> dict[str, Any]:
        """The price and the full set of model inputs behind it."""
        return {
            "value": self.value,
            "unit": "USD",
            "model": self.model,
            "volatility": self.volatility.to_dict(),
            "forward": self.forward,
            "strike": self.strike,
            "expiry": self.expiry,
            "numeraire": self.numeraire,
        }


def _evidence(
    produced_by: str,
    instrument: Any,
    curve_set: CurveSet,
    volatility: Volatility,
    extra: dict[str, Any],
    sources: tuple[Evidence, ...],
) -> Evidence:
    return Evidence(
        produced_by=produced_by,
        fields={
            "instrument": instrument.describe(),
            "model": model_for(volatility),
            "model_note": (
                "The model follows from the units of the quote: an absolute volatility "
                "prices under Bachelier, a relative one under Black. The two are not "
                "convertible, so neither is the choice."
            ),
            "volatility": volatility.to_dict(),
            "curve_interpolation": curve_set.discount.interpolation,
            "dual_curve": curve_set.is_dual,
            "discounting": "collateral_rate_ois_sofr",
            **extra,
        },
        sources=sources,
    )


def swaption_pv(
    swaption: Swaption,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    as_of: date | None = None,
    source_evidence: tuple[Evidence, ...] = (),
) -> OptionPriceResult:
    """Price a European swaption under the model its volatility implies.

    Args:
        swaption: The contract.
        curve_set: Discount and projection curves.
        volatility: The quote. Its units pick the model.
        as_of: Valuation date. Defaults to the curve's.
        source_evidence: Evidence of the curves and the volatility surface.

    Returns:
        The :class:`OptionPriceResult`.

    Raises:
        ShiftRequiredError: Black was implied and the forward is not positive.
    """
    valuation = as_of or curve_set.as_of
    forward = forward_swap_rate(swaption, curve_set)
    numeraire = swaption_annuity(swaption, curve_set)
    expiry = swaption.time_to_expiry(valuation)
    value = _price(volatility, forward, swaption.strike, expiry, swaption.kind, numeraire)
    return OptionPriceResult(
        evidence=_evidence(
            "optionpricing.swaption_pv",
            swaption,
            curve_set,
            volatility,
            {
                "forward": forward,
                "strike": swaption.strike,
                "expiry_years": expiry,
                "annuity": numeraire,
                "numeraire_note": (
                    "The swap annuity. Under the annuity measure the forward swap rate "
                    "is a martingale, which is what makes a one-dimensional option "
                    "formula correct here rather than merely convenient."
                ),
            },
            source_evidence,
        ),
        value=value,
        model=model_for(volatility),
        volatility=volatility,
        forward=forward,
        strike=swaption.strike,
        expiry=expiry,
        numeraire=numeraire,
    )


def caplet_pv(
    caplet: Caplet,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    as_of: date | None = None,
) -> float:
    """Price one caplet or floorlet, in USD.

    Args:
        caplet: The period.
        curve_set: Discount and projection curves.
        volatility: The quote for this period.
        as_of: Valuation date. Defaults to the curve's.

    Returns:
        The present value in USD.

    Raises:
        MissingForwardError: The period ends past the projection curve.
    """
    from rates_engine.conventions.daycount import year_fraction
    from rates_engine.curves.discount import CURVE_TIME_BASIS

    valuation = as_of or curve_set.as_of
    expiry = max(year_fraction(valuation, caplet.accrual_start, CURVE_TIME_BASIS), 0.0)
    return _price(
        volatility,
        caplet.forward_rate(curve_set),
        caplet.strike,
        expiry,
        caplet.kind,
        caplet.numeraire(curve_set),
    )


def cap_floor_pv(
    cap: CapFloor,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    as_of: date | None = None,
    source_evidence: tuple[Evidence, ...] = (),
) -> OptionPriceResult:
    """Price a cap or floor as the sum of its periods.

    A single flat volatility across the strip, which is what a cap is quoted
    on. A caplet-by-caplet surface is a different product quote and a
    different function.

    Args:
        cap: The contract.
        curve_set: Discount and projection curves.
        volatility: The flat quote. Its units pick the model.
        as_of: Valuation date. Defaults to the curve's.
        source_evidence: Evidence of the curves and the surface.

    Returns:
        The :class:`OptionPriceResult`. ``forward`` and ``expiry`` report the
        last period's, since a strip has no single one; the per-period detail
        is in the evidence.

    Raises:
        MissingForwardError: A period ends past the projection curve.
    """
    valuation = as_of or curve_set.as_of
    caplets = cap.caplets
    if not caplets:
        raise ValueError("a cap with no periods has nothing to price")
    values = [caplet_pv(c, curve_set, volatility, as_of=valuation) for c in caplets]
    forwards = [c.forward_rate(curve_set) for c in caplets]
    numeraires = [c.numeraire(curve_set) for c in caplets]

    from rates_engine.conventions.daycount import year_fraction
    from rates_engine.curves.discount import CURVE_TIME_BASIS

    expiries = [
        max(year_fraction(valuation, c.accrual_start, CURVE_TIME_BASIS), 0.0) for c in caplets
    ]
    return OptionPriceResult(
        evidence=_evidence(
            "optionpricing.cap_floor_pv",
            cap,
            curve_set,
            volatility,
            {
                "periods": len(caplets),
                "flat_volatility": True,
                "per_period": [
                    {
                        **c.describe(),
                        "forward": f,
                        "expiry_years": t,
                        "numeraire": n,
                        "value": v,
                    }
                    for c, f, t, n, v in zip(
                        caplets, forwards, expiries, numeraires, values, strict=True
                    )
                ],
            },
            source_evidence,
        ),
        value=sum(values),
        model=model_for(volatility),
        volatility=volatility,
        forward=forwards[-1],
        strike=cap.strike,
        expiry=expiries[-1],
        numeraire=sum(numeraires),
    )
