"""Pricing the option instruments, and the model choice that has to be declared.

Kept apart from :mod:`rates_engine.pricing.linear` because the two answer different
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

**What has already happened is not an option.** A cap or floor valued after
it started has these kinds of period, by QuantLib's rule with its default
``enforcesTodaysHistoricFixings = False``:

- paid on or before the valuation date: gone, and not valued;
- fixed *before* the valuation date and still to be paid: its intrinsic
  value on the *published* fixing -- passed as ``fixings``, a
  :class:`~rates_engine.market.snapshot.MarketSnapshot` -- with no time
  value left, and refused without it;
- fixing *on* the valuation date: its intrinsic value on that day's fixing
  when ``fixings`` holds it, and otherwise on the curve's forward with no
  time left -- the rate is being published that day, and the forward is
  the curve's estimate of it. The evidence says which of the two it was;
- the rest are options, to their fixing dates.

v0.3 had no notion of any of it: the curve's discount factor is one on and
before its date, so a floor from 2025-01-15 valued on 2026-01-15 counted
231,666.67 of periods paid in 2025 inside a 314,181.72 price, and the
running period of a cap was priced off a "forward" read across the
valuation date -- 2.61% on a 4% curve.

A swaption expiring on or before the valuation date is refused: it is no
longer an option. An exercise dated the valuation date has happened, as a
payment dated that day has been made -- one convention for every event, which
is QuantLib's default ``includeReferenceDateEvents = False``, under which
such a swaption is worth nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.errors import MissingFixingError, MissingForwardError
from rates_engine.core.evidence import Evidence, Provenance
from rates_engine.core.money import require_same_currency
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.capfloor import CapFloor, Caplet
from rates_engine.instruments.swaption import Swaption
from rates_engine.market.snapshot import MarketSnapshot
from rates_engine.models import bachelier, black
from rates_engine.pricing.collateral import discounting_fields, valuation_warnings
from rates_engine.volatility.units import Volatility

__all__ = [
    "caplet_forward_rate",
    "caplet_numeraire",
    "OptionPriceResult",
    "model_for",
    "option_value",
    "forward_swap_rate",
    "swaption_annuity",
    "swaption_pv",
    "PeriodStatus",
    "CapletValuation",
    "cap_floor_periods",
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
    from rates_engine.pricing.linear import par_rate

    return par_rate(swaption.underlying, curve_set).value


def swaption_annuity(swaption: Swaption, curve_set: CurveSet) -> float:
    """The numeraire: discounted fixed-leg accrual times notional, in the swap's currency.

    Args:
        swaption: The contract.
        curve_set: Discount and projection curves.

    Returns:
        The annuity in the underlying swap's currency per unit of rate --
        USD for a SOFR swaption, MXN for a TIIE one.
    """
    from rates_engine.pricing.linear import annuity as leg_annuity

    return leg_annuity(swaption.underlying, curve_set).value * swaption.notional


def caplet_forward_rate(caplet: Caplet, curve_set: CurveSet) -> float:
    """The projected rate for one caplet's period, as a decimal.

    Args:
        caplet: The period.
        curve_set: Discount and projection curves.

    Returns:
        The forward from the projection curve, on the caplet's day count.

    Raises:
        MissingForwardError: The period ends past the projection curve's last
            node, where a forward would be extrapolated rather than implied.
        MissingFixingError: The period began before the projection curve's
            date. The curve's discount factor is one on and before that
            date, so the ratio would be the rate from the curve's date to
            the end spread over the whole accrual -- 2.61% on a 4% curve for
            a quarter one month in -- and the period's rate is a fixing.
    """
    require_same_currency(
        caplet.currency, curve_set.currency, operation="projecting a caplet's forward"
    )
    projection = curve_set.projection
    if caplet.accrual_start < projection.as_of:
        raise MissingFixingError(
            f"the period from {caplet.accrual_start} to {caplet.accrual_end} began before "
            f"the projection curve's date {projection.as_of}, so it has no forward: its "
            f"rate is the {caplet.index.label} fixing of {caplet.fixing_date}. Price it "
            "with cap_floor_pv or caplet_pv and pass fixings=, which value it on that "
            "fixing."
        )
    if caplet.accrual_end > projection.nodes[-1]:
        raise MissingForwardError(
            f"the caplet over {caplet.accrual_start} to {caplet.accrual_end} ends past the "
            f"projection curve's last node {projection.nodes[-1]}. The curve would "
            "extrapolate a flat forward there; for a discount factor that is a "
            "convention, for an option it is a rate the market never quoted."
        )
    return projection.forward(caplet.accrual_start, caplet.accrual_end, day_count=caplet.day_count)


def caplet_numeraire(caplet: Caplet, curve_set: CurveSet) -> float:
    """The discounted accrual one caplet's payoff is scaled by.

    Args:
        caplet: The period.
        curve_set: Discount and projection curves.

    Returns:
        ``notional * year_fraction * P(payment)``, in the caplet's currency.
    """
    require_same_currency(
        caplet.currency, curve_set.currency, operation="discounting a caplet's payoff"
    )
    return caplet.notional * caplet.year_fraction * curve_set.discount.df(caplet.payment)


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


def option_value(
    volatility: Volatility,
    forward: float,
    strike: float,
    expiry: float,
    kind: OptionKind,
    numeraire: float,
) -> float:
    """One option's price under the model its quote implies.

    The one place a volatility meets a formula: Bachelier for a normal
    quote, Black for a lognormal one. Every pricer here and every greek
    goes through it, so a bump and the value it bumps cannot disagree about
    the model.

    Args:
        volatility: The quote; its units pick the model.
        forward: The forward rate as a decimal.
        strike: The strike as a decimal.
        expiry: Time to expiry in years, on the curve's time basis.
        kind: Call or put on the rate.
        numeraire: The annuity or discounted accrual the payoff is scaled by,
            in the currency of the result.

    Returns:
        The price in the units of ``numeraire``.

    Raises:
        ShiftRequiredError: The quote is lognormal and the forward is not
            positive.
    """
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
        value: Present value, in :attr:`currency`.
        model: ``"bachelier"`` or ``"black"``.
        volatility: The quote used, carrying its units.
        forward: The forward rate the option is on, as a decimal.
        strike: The strike as a decimal.
        expiry: Time to expiry in years.
        numeraire: The annuity or discounted accrual the payoff scaled by,
            in :attr:`currency` per unit of rate.
        currency: What :attr:`value` and :attr:`numeraire` are in, taken
            from the curves the option was priced on -- the payload's
            ``unit`` used to say ``"USD"`` for a peso swaption too.
    """

    value: float
    model: str
    volatility: Volatility
    forward: float
    strike: float
    expiry: float
    numeraire: float
    currency: str

    def payload_fields(self) -> dict[str, Any]:
        """The price and the full set of model inputs behind it."""
        return {
            "value": self.value,
            "unit": self.currency,
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
    inputs: tuple[Provenance, ...] = (),
) -> Evidence:
    return Evidence(
        produced_by=produced_by,
        inputs=inputs,
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
            "discounting": discounting_fields(curve_set.currency)["discounting"],
            **extra,
        },
        sources=sources,
        warnings=valuation_warnings(instrument, curve_set),
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
        ValueError: The swaption expires on or before the valuation date. It
            is no longer an option: value the swap it became, or nothing. An
            exercise dated the valuation date has happened, as a payment
            dated that day has been made.
        ShiftRequiredError: Black was implied and the forward is not positive.
    """
    valuation = as_of or curve_set.as_of
    if swaption.expiry <= valuation:
        raise ValueError(
            f"the swaption expired on {swaption.expiry}, on or before the valuation date "
            f"{valuation}: it is no longer an option. If it was exercised, value the swap "
            "it became with pv(); if it lapsed it is worth nothing. An exercise dated the "
            "valuation date counts as done, as a payment dated that day does."
        )
    forward = forward_swap_rate(swaption, curve_set)
    numeraire = swaption_annuity(swaption, curve_set)
    expiry = swaption.time_to_expiry(valuation)
    value = option_value(volatility, forward, swaption.strike, expiry, swaption.kind, numeraire)
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
        currency=curve_set.currency.value,
    )


class PeriodStatus(StrEnum):
    """Where one period of a cap or floor stands on the valuation date.

    ``PAID``
        Paid on or before the valuation date. Gone, and not valued.
    ``FIXED``
        Still to be paid, and valued on its published fixing: one dated
        before the valuation date, which is required, or one dated on it
        that the fixings supplied. Nothing optional is left: it is worth its
        intrinsic value on that fixing.
    ``FIXES_TODAY``
        Fixes on the valuation date and the fixings did not hold that day's
        rate. Valued on the curve's forward with no time left -- its
        intrinsic value on the forward -- as QuantLib does when today's
        fixing is not in the history.
    ``LIVE``
        Fixes after the valuation date. An option.
    """

    PAID = "paid"
    FIXED = "fixed"
    FIXES_TODAY = "fixes_today"
    LIVE = "live"


@dataclass(frozen=True)
class CapletValuation:
    """One unpaid period of a cap or floor, valued on the date asked for.

    Attributes:
        caplet: The period's terms.
        status: ``FIXED``, ``FIXES_TODAY`` or ``LIVE``; a paid period is
            never valued.
        forward: The projected rate as a decimal for a live period or one
            fixing today without its fixing, ``None`` for a fixed one, whose
            rate is known.
        fixing: The published fixing as a decimal for a fixed period,
            ``None`` otherwise.
        expiry: Years from the valuation date to the fixing, on the curve's
            time basis; zero for a fixed period and one fixing today.
        numeraire: ``notional * year_fraction * P(payment)``, in the
            caplet's currency.
        value: Present value in the caplet's currency.
    """

    caplet: Caplet
    status: PeriodStatus
    forward: float | None
    fixing: float | None
    expiry: float
    numeraire: float
    value: float

    def describe(self) -> dict[str, Any]:
        """The period's terms and how it was valued, for the evidence record."""
        return {
            **self.caplet.describe(),
            "status": self.status.value,
            "fixing_source": _FIXING_SOURCE[self.status],
            "forward": self.forward,
            "fixing": self.fixing,
            "expiry_years": self.expiry,
            "numeraire": self.numeraire,
            "value": self.value,
        }


_FIXING_SOURCE: dict[PeriodStatus, str | None] = {
    PeriodStatus.FIXED: "published",
    PeriodStatus.FIXES_TODAY: "curve_forward_on_fixing_day",
    PeriodStatus.LIVE: None,
    PeriodStatus.PAID: None,
}
"""What a period's rate was taken from, for the evidence: the fixing, the curve, or neither yet."""


def _status(caplet: Caplet, valuation: date) -> PeriodStatus:
    """Paid, fixed before the valuation date, fixing on it, or live.

    ``FIXES_TODAY`` is provisional: it becomes ``FIXED`` when the fixings
    hold that day's rate.
    """
    if caplet.payment <= valuation:
        return PeriodStatus.PAID
    if caplet.fixing_date < valuation:
        return PeriodStatus.FIXED
    if caplet.fixing_date == valuation:
        return PeriodStatus.FIXES_TODAY
    return PeriodStatus.LIVE


def _period_name(caplet: Caplet, valuation: date) -> str:
    kind = "caplet" if caplet.kind is OptionKind.CALL else "floorlet"
    return (
        f"the {kind} from {caplet.accrual_start} to {caplet.accrual_end}, paid "
        f"{caplet.payment}, fixed on {caplet.fixing_date}, before the valuation "
        f"date {valuation}"
    )


def _finite_fixing(caplet: Caplet, rate: float, what: str) -> float:
    if not math.isfinite(rate):
        raise MissingFixingError(
            f"{what}, and its fixing in the series {caplet.index.name!r} is {rate!r}, not "
            "a finite number. A NaN here is usually a gap in the source file."
        )
    return rate


def _todays_fixing(
    caplet: Caplet, valuation: date, fixings: MarketSnapshot | None
) -> tuple[float, Provenance] | None:
    """The fixing published on the valuation date, or ``None`` when it was not supplied.

    Absent is not an error on the fixing day itself: the rate is being
    published that day, so the curve's forward stands in for it -- QuantLib's
    default, ``enforcesTodaysHistoricFixings = False`` -- and the period says
    so in the evidence. A fixing that is there and is not a number is still
    refused.
    """
    if fixings is None:
        return None
    series_id = caplet.index.name
    try:
        rate = fixings.fixing(series_id, caplet.fixing_date)
    except MissingFixingError:
        # Not published yet, or not supplied: the documented fallback above,
        # recorded per period as fixing_source = curve_forward_on_fixing_day.
        return None
    what = (
        f"the period from {caplet.accrual_start} to {caplet.accrual_end} fixes on the "
        f"valuation date {valuation}"
    )
    return _finite_fixing(caplet, rate, what), fixings.require(series_id).provenance


def _published_fixing(
    caplet: Caplet, valuation: date, fixings: MarketSnapshot | None
) -> tuple[float, Provenance]:
    """The published rate of a period fixed before the valuation date; never projected."""
    series_id = caplet.index.name
    what = _period_name(caplet, valuation)
    if fixings is None:
        raise MissingFixingError(
            f"{what}: its rate is a published {caplet.index.label} fixing now, not a "
            "forward, and nothing about it is optional. Pass fixings=MarketSnapshot(...) "
            f"holding a series {series_id!r} with the {caplet.fixing_date} fixing as a "
            f"decimal, or value the strip from a date before {caplet.fixing_date}."
        )
    try:
        rate = fixings.fixing(series_id, caplet.fixing_date)
    except MissingFixingError as missing:
        raise MissingFixingError(
            f"{what}, and the fixings passed do not have it ({missing}). Add the "
            f"{caplet.fixing_date} fixing to the series {series_id!r}, as a decimal."
        ) from missing
    return _finite_fixing(caplet, rate, what), fixings.require(series_id).provenance


def _value_period(
    caplet: Caplet,
    curve_set: CurveSet,
    volatility: Volatility,
    valuation: date,
    fixings: MarketSnapshot | None,
) -> tuple[CapletValuation | None, Provenance | None]:
    """Value one period, or ``None`` when it was paid; with the fixing's provenance."""
    status = _status(caplet, valuation)
    if status is PeriodStatus.PAID:
        return None, None
    numeraire = caplet_numeraire(caplet, curve_set)
    published: tuple[float, Provenance] | None = None
    if status is PeriodStatus.FIXED:
        published = _published_fixing(caplet, valuation, fixings)
    elif status is PeriodStatus.FIXES_TODAY:
        published = _todays_fixing(caplet, valuation, fixings)
        if published is None:
            forward = caplet_forward_rate(caplet, curve_set)
            return (
                CapletValuation(
                    caplet=caplet,
                    status=status,
                    forward=forward,
                    fixing=None,
                    expiry=0.0,
                    numeraire=numeraire,
                    value=numeraire * max(caplet.kind.sign * (forward - caplet.strike), 0.0),
                ),
                None,
            )
    if published is not None:
        rate, provenance = published
        intrinsic = max(caplet.kind.sign * (rate - caplet.strike), 0.0)
        return (
            CapletValuation(
                caplet=caplet,
                status=PeriodStatus.FIXED,
                forward=None,
                fixing=rate,
                expiry=0.0,
                numeraire=numeraire,
                value=numeraire * intrinsic,
            ),
            provenance,
        )
    forward = caplet_forward_rate(caplet, curve_set)
    expiry = caplet.time_to_expiry(valuation)
    return (
        CapletValuation(
            caplet=caplet,
            status=status,
            forward=forward,
            fixing=None,
            expiry=expiry,
            numeraire=numeraire,
            value=option_value(
                volatility, forward, caplet.strike, expiry, caplet.kind, numeraire
            ),
        ),
        None,
    )


def caplet_pv(
    caplet: Caplet,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    as_of: date | None = None,
    fixings: MarketSnapshot | None = None,
) -> float:
    """Price one caplet or floorlet, in the caplet's currency.

    Args:
        caplet: The period.
        curve_set: Discount and projection curves.
        volatility: The quote for this period.
        as_of: Valuation date. Defaults to the curve's.
        fixings: Published fixings, as decimals, in a series named after the
            caplet's index (``"TERM_SOFR_3M"``). Required when the period
            fixed before the valuation date; read, if present, when it fixes
            on it.

    Returns:
        The present value in the caplet's currency: zero for a period paid
        on or before the valuation date; the discounted intrinsic value on
        the published fixing for one that has fixed, or on the curve's
        forward for one fixing on the valuation date whose fixing was not
        supplied; and the option price to the fixing date otherwise.

    Raises:
        MissingForwardError: The period ends past the projection curve.
        MissingFixingError: The period fixed before the valuation date and
            ``fixings`` does not hold its fixing, or a fixing it holds is
            not a finite number.
    """
    valuation = as_of or curve_set.as_of
    period, _ = _value_period(caplet, curve_set, volatility, valuation, fixings)
    return 0.0 if period is None else period.value


def cap_floor_periods(
    cap: CapFloor,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    as_of: date | None = None,
    fixings: MarketSnapshot | None = None,
) -> tuple[CapletValuation, ...]:
    """Value a cap or floor period by period, leaving out what has been paid.

    The breakdown :func:`cap_floor_pv` sums, and what the greeks bump one
    period at a time.

    Args:
        cap: The contract.
        curve_set: Discount and projection curves.
        volatility: The flat quote. Its units pick the model.
        as_of: Valuation date. Defaults to the curve's.
        fixings: Published fixings for the periods that have fixed; see
            :func:`caplet_pv`.

    Returns:
        One :class:`CapletValuation` per period still to be paid, in
        schedule order. Empty when every period has been paid.

    Raises:
        MissingForwardError: A live period ends past the projection curve.
        MissingFixingError: A period fixed before the valuation date and
            ``fixings`` does not hold its fixing.
    """
    valuation = as_of or curve_set.as_of
    periods = []
    for caplet in cap.caplets:
        period, _ = _value_period(caplet, curve_set, volatility, valuation, fixings)
        if period is not None:
            periods.append(period)
    return tuple(periods)


def cap_floor_pv(
    cap: CapFloor,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    as_of: date | None = None,
    fixings: MarketSnapshot | None = None,
    source_evidence: tuple[Evidence, ...] = (),
) -> OptionPriceResult:
    """Price a cap or floor as the sum of its periods.

    A single flat volatility across the strip, which is what a cap is quoted
    on. A caplet-by-caplet surface is a different product quote and a
    different function. A strip that has started is valued on what has
    happened: see the module docstring.

    Args:
        cap: The contract.
        curve_set: Discount and projection curves.
        volatility: The flat quote. Its units pick the model.
        as_of: Valuation date. Defaults to the curve's.
        fixings: Published fixings, as decimals, in a series named after the
            cap's index (``"TERM_SOFR_3M"``). Required for a period still
            to be paid that fixed before the valuation date; read, if
            present, for one fixing on it; never read by a strip that fixes
            later.
        source_evidence: Evidence of the curves and the surface.

    Returns:
        The :class:`OptionPriceResult`. ``forward`` and ``expiry`` report the
        last period's -- its fixing and zero if it has fixed -- since a strip
        has no single one; the per-period detail is in the evidence.

    Raises:
        ValueError: The strip has no periods, or every one has been paid.
        MissingForwardError: A live period ends past the projection curve.
        MissingFixingError: A period fixed before the valuation date and
            ``fixings`` does not hold its fixing; the message names the
            series and the date.
    """
    valuation = as_of or curve_set.as_of
    caplets = cap.caplets
    if not caplets:
        raise ValueError("a cap with no periods has nothing to price")
    periods: list[CapletValuation] = []
    inputs: list[Provenance] = []
    for caplet in caplets:
        period, provenance = _value_period(caplet, curve_set, volatility, valuation, fixings)
        if period is None:
            continue
        periods.append(period)
        if provenance is not None and provenance not in inputs:
            inputs.append(provenance)
    if not periods:
        raise ValueError(
            f"every period of this {cap.product} was paid on or before {valuation}, the "
            f"last on {caplets[-1].payment}; there is nothing left to value."
        )
    last = periods[-1]
    last_rate = last.forward if last.forward is not None else last.fixing
    assert last_rate is not None  # a valued period has one or the other
    return OptionPriceResult(
        evidence=_evidence(
            "optionpricing.cap_floor_pv",
            cap,
            curve_set,
            volatility,
            {
                "periods": len(periods),
                "periods_paid": len(caplets) - len(periods),
                "flat_volatility": True,
                "fixing_lag_days": cap.fixing_lag_days,
                "expiry_note": (
                    "Each caplet expires at its fixing, fixing_lag_days business days "
                    "of the index's calendar before its period starts -- two for Term "
                    "SOFR, the ARRC convention -- not at the start of its accrual."
                ),
                "seasoning_note": (
                    "A period paid on or before the valuation date is not valued. One "
                    "fixed before it is its intrinsic value on the published fixing. One "
                    "fixing on it is its intrinsic value on that day's fixing if supplied, "
                    "else on the curve's forward (fixing_source says which). Only the "
                    "rest are options."
                ),
                "per_period": [p.describe() for p in periods],
            },
            source_evidence,
            inputs=tuple(inputs),
        ),
        value=sum(p.value for p in periods),
        model=model_for(volatility),
        volatility=volatility,
        forward=last_rate,
        strike=cap.strike,
        expiry=last.expiry,
        numeraire=sum(p.numeraire for p in periods),
        currency=curve_set.currency.value,
    )
