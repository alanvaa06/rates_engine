"""Projecting an instrument's cashflows off a curve set: the one place terms meet curves.

Until v0.4 every linear instrument carried a ``cashflows(curve_set)`` method,
so the product layer imported the curve layer and a trade's terms could not
be described without a valuation model beside them. Projection lives here
now. An instrument states what was traded -- dates, rates, a side, the
:class:`~rates_engine.conventions.indices.RateIndex` it floats on -- and
:func:`project` turns that into dated amounts under a given curve set.

**Adding a product.** Write the dataclass in :mod:`rates_engine.instruments`
and register its projection::

    @project.register
    def _(instrument: MyProduct, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        ...

Every pricer, risk measure and hedge then works on it unchanged, because they
all price through :func:`project`. The pricers pass ``fixings=`` on only
when their caller hands them some, so a projection written without the
keyword keeps working; declare ``*, fixings=None`` to accept them.

**Currency.** Every flow carries its instrument's currency, taken from the
index. :func:`rates_engine.pricing.linear.pv` discounts each flow on the
curve set and refuses one in another currency, so a peso swap on the dollar
curve raises :class:`~rates_engine.core.errors.CurrencyMismatchError` rather
than being quietly relabelled -- which is what v0.3 did, by taking the
currency from the curve.

**History.** A period paid on or before the curve set's ``as_of`` is not
projected at all: it has been paid, and nothing about it is worth anything
today. The pricer drops such flows again whatever projected them, so a
product of the caller's own that returns its whole history is still
valued correctly.

A period paid after ``as_of`` whose coupon is already partly or wholly
decided is history, and no curve can say what it was. Its fixed coupon was
known on the trade date and is counted in full. Its floating coupon:

- an overnight rate (SOFR, TIIE de Fondeo) is history once its period has
  begun: the published fixings from the accrual start up to ``as_of``,
  exclusive, are compounded, and the curve's ``P(as_of) / P(end)`` covers
  the rest -- the coupon QuantLib's ``OvernightIndexedCoupon`` pays. A
  period starting on ``as_of`` has no history: the day's rate is published
  the next morning.
- a term rate is history once its fixing date is before ``as_of``, whether
  or not its period has begun, and pays that fixing. A fixing dated
  ``as_of`` itself is used when ``fixings`` holds it and projected off the
  curve when it does not -- QuantLib's default, with
  ``enforcesTodaysHistoricFixings`` off -- so a swap struck at spot today
  still prices without a snapshot; the evidence says which it was. Term
  SOFR fixes two SIFMA business days before the accrual start, the ARRC's
  convention (:data:`_TERM_SOFR_FIXING_LAG_DAYS`): the fixing published on
  2025-10-20 sets the period that starts on the 22nd. TIIE 28 is read on
  the accrual start itself, where this package has always projected it
  from; its real lag is not established
  (``docs/forge/research/003-mxn-conventions.md``) and is pending.

Both come from a :class:`~rates_engine.market.snapshot.MarketSnapshot`
passed as ``fixings``, read by the series named after the index
(``"SOFR"``, ``"TERM_SOFR_3M"``, ``"TIIE_FONDEO"``), and without one the
projection raises :class:`~rates_engine.core.errors.MissingFixingError`
naming what is missing. Until v0.4 it projected the period as
``P(start) / P(end)`` on a curve that is one on or before ``as_of``, so the
part already accrued was worth nothing: a 4% payer over USD 100m started
six months before valuation came back at -2.24m USD against QuantLib's
-17,348. A period with no history is projected exactly as before.
"""

from __future__ import annotations

from datetime import date
from functools import singledispatch
from typing import Any, Protocol, runtime_checkable

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.indices import (
    TERM_SOFR_1M,
    TERM_SOFR_3M,
    TERM_SOFR_6M,
    TERM_SOFR_12M,
    RateIndex,
)
from rates_engine.conventions.side import fixed_leg_sign
from rates_engine.core.errors import MissingFixingError, UnsupportedConventionError
from rates_engine.core.money import Currency
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.cashflow import Cashflow, RealisedFixing
from rates_engine.instruments.fra import FRA
from rates_engine.instruments.swaps import IRSwap, OISSwap
from rates_engine.market.snapshot import MarketSnapshot

__all__ = ["Priceable", "Swappable", "project", "float_leg", "fra_fair_rate"]

_TERM_SOFR_FIXING_LAG_DAYS = 2
"""Business days, on the index's calendar, from a Term SOFR fixing to the
start of the period it sets: the ARRC's convention, and the same one
``instruments/capfloor.py`` applies to caplets as ``TERM_RATE_FIXING_LAG_DAYS``.
Repeated here rather than imported so that the linear pricer does not
depend on the option products. It belongs on :class:`RateIndex`, as a
property of the index rather than of each module that reads it; moving it
there is a follow-up."""

_TERM_SOFR = frozenset(
    index.name for index in (TERM_SOFR_1M, TERM_SOFR_3M, TERM_SOFR_6M, TERM_SOFR_12M)
)


@runtime_checkable
class Priceable(Protocol):
    """Anything :func:`project` has a registration for."""

    @property
    def span(self) -> tuple[date, date]:
        """First and last date the instrument touches, as a half-open interval."""
        ...

    @property
    def currency(self) -> Currency:
        """The currency every one of its flows is paid in."""
        ...

    def describe(self) -> dict[str, Any]:
        """Instrument terms for the evidence record."""
        ...


@runtime_checkable
class Swappable(Priceable, Protocol):
    """A two-legged instrument, which is what a par rate and an annuity need.

    ``notional`` and ``side`` are declared as read-only properties rather than
    attributes, because every instrument in this package is a frozen
    dataclass and a mutable attribute in the protocol would exclude all of
    them. ``with_terms`` is here for the same reason: a par rate is computed
    from variants of the swap, and ``dataclasses.replace`` on a protocol is
    not something a type checker can verify.
    """

    @property
    def notional(self) -> float:
        """Notional in the instrument's currency."""
        ...

    @property
    def side(self) -> str:
        """``"payer"`` or ``"receiver"``."""
        ...

    def with_terms(self, **changes: object) -> Swappable:
        """A copy with some terms changed."""
        ...

    def fixed_cashflows(self) -> tuple[Cashflow, ...]:
        """The fixed leg alone, which needs no curve."""
        ...


@singledispatch
def project(
    instrument: object, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    """Every cashflow of ``instrument`` still to be paid under ``curve_set``, signed for its side.

    Args:
        instrument: A registered instrument.
        curve_set: Discount and projection curves.
        fixings: Published fixings for a floating period already in progress
            on ``curve_set.as_of``, by index name. Needed only by a trade
            that has started; see the module docstring.

    Returns:
        The flows paid after ``curve_set.as_of``, fixed leg first for a swap.

    Raises:
        TypeError: Nothing is registered for the instrument's type.
        MissingFixingError: A floating period is in progress and ``fixings``
            is absent or lacks a fixing it needs.
    """
    raise TypeError(
        f"no cashflow projection is registered for {type(instrument).__name__}; "
        "register one with rates_engine.pricing.projection.project.register"
    )


@singledispatch
def float_leg(
    swap: object, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    """The floating leg of a swap under ``curve_set``, signed for its side.

    Args:
        swap: A registered swap.
        curve_set: Discount and projection curves.
        fixings: Published fixings for the period in progress, if one is.

    Returns:
        One flow per floating period paid after ``curve_set.as_of``.

    Raises:
        TypeError: Nothing is registered for the swap's type.
        MissingFixingError: A floating period is in progress and ``fixings``
            is absent or lacks a fixing it needs.
    """
    raise TypeError(f"no floating leg is registered for {type(swap).__name__}")


def _in_progress(
    instrument: OISSwap | IRSwap | FRA,
    start: date,
    end: date,
    pay: date,
    as_of: date,
    *,
    fixed_on: date | None = None,
) -> str:
    """How the refusals below name the period they could not value."""
    if isinstance(instrument, FRA):
        trade = f"FRA {instrument.start} to {instrument.end}"
    else:
        trade = f"{type(instrument).__name__} {instrument.effective} to {instrument.maturity}"
    when = (
        f"began before the valuation date {as_of}"
        if fixed_on is None
        else f"fixed on {fixed_on}, before the valuation date {as_of}"
    )
    return f"{trade}: the floating period {start} to {end}, paid {pay}, {when}"


def _term_fixing_date(index: RateIndex, accrual_start: date) -> date:
    """The date a term rate's period is fixed on.

    Term SOFR: :data:`_TERM_SOFR_FIXING_LAG_DAYS` business days of its own
    calendar before the accrual start. Any other term rate -- TIIE 28 --
    the accrual start itself, which is where the forward has always been
    taken from and which nothing here has verified against its
    administrator.
    """
    if index.name in _TERM_SOFR:
        return index.calendar.add_business_days(accrual_start, -_TERM_SOFR_FIXING_LAG_DAYS)
    return accrual_start


def _fixing_rule(index: RateIndex) -> str:
    if index.name in _TERM_SOFR:
        return f"{_TERM_SOFR_FIXING_LAG_DAYS} business days before its accrual start"
    return "its accrual start"


def _how_to_pass(index: RateIndex) -> str:
    return (
        f"Pass the published fixings as fixings=MarketSnapshot(as_of=..., series={{"
        f"{index.name!r}: Series(...)}}) to pv, par_rate, discounted_value or project"
    )


def _compounded_history(
    index: RateIndex,
    start: date,
    until: date,
    fixings: MarketSnapshot | None,
    context: str,
) -> RealisedFixing:
    """The overnight fixings of ``[start, until)`` compounded, and the record of them."""
    if fixings is None:
        raise MissingFixingError(
            f"{context}, so part of its coupon is {index.name} compounded from {start} up to "
            f"{until} (exclusive): history no curve knows. {_how_to_pass(index)}."
        )
    # MarketSnapshot.compounded multiplies 1 + r/360 a day. An overnight
    # index accruing on another basis needs another rule, and this one would
    # be off by the ratio of the two bases without a word.
    if index.day_count is not DayCount.ACT_360:
        raise UnsupportedConventionError(
            f"{context}; its realised part would be compounded on ACT/360, and {index.name} "
            f"accrues on {index.day_count.value}. Only ACT/360 overnight indices can be "
            "valued part-way through a period."
        )
    try:
        series = fixings.require(index.name)
        compounded = fixings.compounded(index.name, start, until, calendar=index.calendar)
    except MissingFixingError as error:
        raise MissingFixingError(f"{context}: {error}") from error
    return RealisedFixing(
        series_id=index.name,
        method="compounded",
        observed_from=start,
        observed_to=until,
        rate=compounded.rate,
        accrual_factor=compounded.accrual_factor,
        business_days=compounded.business_days,
        repeated_days=compounded.repeated_days,
        provenance=series.provenance,
    )


def _term_history(
    instrument: IRSwap | FRA,
    index: RateIndex,
    start: date,
    end: date,
    pay: date,
    as_of: date,
    fixings: MarketSnapshot | None,
) -> tuple[RealisedFixing | None, date | None]:
    """What a term period's rate rests on: a published fixing, or nothing yet.

    Returns the record of the fixing to pay, or ``None`` with the curve to
    forecast instead; and, when that forecast stands in for a fixing dated
    ``as_of`` that ``fixings`` does not hold, that date, so the evidence can
    say so. A fixing date before ``as_of`` is history and must be supplied.
    One dated ``as_of`` is used if supplied and forecast if not, which is
    QuantLib's default: the day's print may not be out when the book is
    valued, and refusing a swap struck at spot this morning would be a
    refusal of the ordinary case.
    """
    fixing_date = _term_fixing_date(index, start)
    if fixing_date > as_of:
        return None, None
    if fixing_date == as_of:
        series = fixings.series.get(index.name) if fixings is not None else None
        if series is None or series.get(fixing_date) is None:
            return None, fixing_date
    context = _in_progress(instrument, start, end, pay, as_of, fixed_on=fixing_date)
    return _term_fixing(index, fixing_date, fixings, context), None


def _term_fixing(
    index: RateIndex,
    fixing_date: date,
    fixings: MarketSnapshot | None,
    context: str,
) -> RealisedFixing:
    """The term fixing published on ``fixing_date``, and the record of it."""
    if fixings is None:
        raise MissingFixingError(
            f"{context}, so its coupon is the {index.name} fixing of {fixing_date}, "
            f"{_fixing_rule(index)}: history no curve knows. {_how_to_pass(index)}."
        )
    try:
        series = fixings.require(index.name)
        rate = fixings.fixing(index.name, fixing_date)
    except MissingFixingError as error:
        raise MissingFixingError(
            f"{context}, and it fixes {_fixing_rule(index)}: {error}"
        ) from error
    return RealisedFixing(
        series_id=index.name,
        method="term",
        observed_from=fixing_date,
        observed_to=fixing_date,
        rate=rate,
        accrual_factor=None,
        business_days=1,
        repeated_days=0,
        provenance=series.provenance,
    )


@float_leg.register
def _ois_float(
    swap: OISSwap, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    """Compounded overnight, projected off the discount curve.

    The compounded overnight growth over a period is exactly the ratio of the
    period's discount factors on a deterministic curve, so no separate
    projection is needed and none is invented. A period in progress
    compounds its published fixings up to ``as_of`` and the curve from there.
    """
    sign = -fixed_leg_sign(swap.side)
    curve = curve_set.discount
    as_of = curve_set.as_of
    day_count = swap.index.day_count
    flows: list[Cashflow] = []
    schedule = swap.schedule
    for start, end, pay in zip(
        schedule.accrual_start, schedule.accrual_end, schedule.payment, strict=True
    ):
        if pay <= as_of:
            continue
        tau = year_fraction(start, end, day_count)
        realised: RealisedFixing | None = None
        if start < as_of:
            realised = _compounded_history(
                swap.index,
                start,
                min(end, as_of),
                fixings,
                _in_progress(swap, start, end, pay, as_of),
            )
            assert realised.accrual_factor is not None  # set for every compounded record
            remaining = curve.df(as_of) / curve.df(end) if end > as_of else 1.0
            growth = realised.accrual_factor * remaining
        else:
            growth = curve.df(start) / curve.df(end)
        rate = (growth - 1.0) / tau if tau else 0.0
        flows.append(
            Cashflow(
                payment_date=pay,
                amount=sign * swap.notional * (growth - 1.0),
                leg="float",
                accrual_start=start,
                accrual_end=end,
                year_fraction=tau,
                rate=rate,
                currency=swap.currency,
                fixing=realised,
            )
        )
    return tuple(flows)


@float_leg.register
def _irs_float(
    swap: IRSwap, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    """Forwards from the projection curve, nothing else.

    This is the whole multi-curve story in four lines. The forward comes from
    ``curve_set.projection``; the discounting, applied in
    :mod:`rates_engine.pricing.linear`, comes from ``curve_set.discount``.
    When the two are the same object the basis is zero and the result
    collapses onto the OIS answer. A period that has fixed pays its fixing
    instead of a forward; see the module docstring for when that is.
    """
    sign = -fixed_leg_sign(swap.side)
    projection = curve_set.projection
    as_of = curve_set.as_of
    flows: list[Cashflow] = []
    schedule = swap.float_schedule
    for start, end, pay in zip(
        schedule.accrual_start, schedule.accrual_end, schedule.payment, strict=True
    ):
        if pay <= as_of:
            continue
        tau = year_fraction(start, end, swap.float_day_count)
        realised, projected_on = _term_history(
            swap, swap.float_index, start, end, pay, as_of, fixings
        )
        if realised is not None:
            rate = realised.rate
        else:
            rate = projection.forward(start, end, day_count=swap.float_day_count)
        flows.append(
            Cashflow(
                payment_date=pay,
                amount=sign * swap.notional * rate * tau,
                leg="float",
                accrual_start=start,
                accrual_end=end,
                year_fraction=tau,
                rate=rate,
                currency=swap.currency,
                fixing=realised,
                projected_fixing_date=projected_on,
            )
        )
    return tuple(flows)


def _unpaid_fixed(swap: OISSwap | IRSwap, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    """The fixed coupons still to be paid, the one accruing today in full.

    A fixed coupon is known from the trade date, so the period in progress
    needs nothing but its own terms.
    """
    as_of = curve_set.as_of
    return tuple(flow for flow in swap.fixed_cashflows() if flow.payment_date > as_of)


@project.register
def _project_ois(
    swap: OISSwap, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    return _unpaid_fixed(swap, curve_set) + _ois_float(swap, curve_set, fixings=fixings)


@project.register
def _project_irs(
    swap: IRSwap, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    return _unpaid_fixed(swap, curve_set) + _irs_float(swap, curve_set, fixings=fixings)


def _fra_rate(
    fra: FRA, curve_set: CurveSet, fixings: MarketSnapshot | None
) -> tuple[float, RealisedFixing | None, date | None]:
    """The rate the FRA settles on, the fixing behind it, and a fixing date projected over."""
    realised, projected_on = _term_history(
        fra, fra.index, fra.start, fra.end, fra.end, curve_set.as_of, fixings
    )
    if realised is not None:
        return realised.rate, realised, None
    forward = curve_set.projection.forward(fra.start, fra.end, day_count=fra.day_count)
    return forward, None, projected_on


def fra_fair_rate(
    fra: FRA, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> float:
    """The rate that makes the FRA worth zero, from the projection curve.

    Once the index has fixed the rate is no longer a forecast: it is the
    fixing, read from ``fixings``. Term SOFR fixes two business days before
    the period starts, TIIE 28 on the start itself (a convention still to be
    verified). A fixing dated ``curve_set.as_of`` is used when ``fixings``
    holds it and forecast when it does not, as QuantLib does.

    Args:
        fra: The contract.
        curve_set: Discount and projection curves.
        fixings: Published fixings, needed once the fixing date is before
            ``curve_set.as_of`` and read, if present, when it is that date.
            The series is named after the index, e.g. ``"TERM_SOFR_3M"``.

    Returns:
        The fair rate as a decimal on the FRA's day count.

    Raises:
        MissingFixingError: The index fixed before ``curve_set.as_of`` and
            ``fixings`` does not hold that fixing.
    """
    rate, _, _ = _fra_rate(fra, curve_set, fixings)
    return rate


@project.register
def _project_fra(
    fra: FRA, curve_set: CurveSet, *, fixings: MarketSnapshot | None = None
) -> tuple[Cashflow, ...]:
    if fra.end <= curve_set.as_of:
        return ()
    sign = -fixed_leg_sign(fra.side)
    settled, realised, projected_on = _fra_rate(fra, curve_set, fixings)
    tau = fra.year_fraction
    return (
        Cashflow(
            payment_date=fra.end,
            amount=sign * fra.notional * (settled - fra.rate) * tau,
            leg="float",
            accrual_start=fra.start,
            accrual_end=fra.end,
            year_fraction=tau,
            rate=settled,
            currency=fra.currency,
            fixing=realised,
            projected_fixing_date=projected_on,
        ),
    )
