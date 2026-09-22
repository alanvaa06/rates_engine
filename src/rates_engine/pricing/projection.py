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
all price through :func:`project`.

**Currency.** Every flow carries its instrument's currency, taken from the
index. :func:`rates_engine.pricing.linear.pv` discounts each flow on the
curve set and refuses one in another currency, so a peso swap on the dollar
curve raises :class:`~rates_engine.core.errors.CurrencyMismatchError` rather
than being quietly relabelled -- which is what v0.3 did, by taking the
currency from the curve.
"""

from __future__ import annotations

from datetime import date
from functools import singledispatch
from typing import Any, Protocol, runtime_checkable

from rates_engine.conventions.daycount import year_fraction
from rates_engine.conventions.side import fixed_leg_sign
from rates_engine.core.money import Currency
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.instruments.fra import FRA
from rates_engine.instruments.swaps import IRSwap, OISSwap

__all__ = ["Priceable", "Swappable", "project", "float_leg", "fra_fair_rate"]


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
def project(instrument: object, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    """Every cashflow of ``instrument`` under ``curve_set``, signed for its side.

    Args:
        instrument: A registered instrument.
        curve_set: Discount and projection curves.

    Returns:
        The flows, fixed leg first for a swap.

    Raises:
        TypeError: Nothing is registered for the instrument's type.
    """
    raise TypeError(
        f"no cashflow projection is registered for {type(instrument).__name__}; "
        "register one with rates_engine.pricing.projection.project.register"
    )


@singledispatch
def float_leg(swap: object, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    """The floating leg of a swap under ``curve_set``, signed for its side.

    Args:
        swap: A registered swap.
        curve_set: Discount and projection curves.

    Returns:
        One flow per floating period.

    Raises:
        TypeError: Nothing is registered for the swap's type.
    """
    raise TypeError(f"no floating leg is registered for {type(swap).__name__}")


@float_leg.register
def _ois_float(swap: OISSwap, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    """Compounded overnight, projected off the discount curve.

    The compounded overnight growth over a period is exactly the ratio of the
    period's discount factors on a deterministic curve, so no separate
    projection is needed and none is invented.
    """
    sign = -fixed_leg_sign(swap.side)
    curve = curve_set.discount
    day_count = swap.index.day_count
    flows: list[Cashflow] = []
    schedule = swap.schedule
    for start, end, pay in zip(
        schedule.accrual_start, schedule.accrual_end, schedule.payment, strict=True
    ):
        tau = year_fraction(start, end, day_count)
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
            )
        )
    return tuple(flows)


@float_leg.register
def _irs_float(swap: IRSwap, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    """Forwards from the projection curve, nothing else.

    This is the whole multi-curve story in four lines. The forward comes from
    ``curve_set.projection``; the discounting, applied in
    :mod:`rates_engine.pricing.linear`, comes from ``curve_set.discount``.
    When the two are the same object the basis is zero and the result
    collapses onto the OIS answer.
    """
    sign = -fixed_leg_sign(swap.side)
    projection = curve_set.projection
    flows: list[Cashflow] = []
    schedule = swap.float_schedule
    for start, end, pay in zip(
        schedule.accrual_start, schedule.accrual_end, schedule.payment, strict=True
    ):
        tau = year_fraction(start, end, swap.float_day_count)
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
            )
        )
    return tuple(flows)


@project.register
def _project_ois(swap: OISSwap, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    return swap.fixed_cashflows() + _ois_float(swap, curve_set)


@project.register
def _project_irs(swap: IRSwap, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    return swap.fixed_cashflows() + _irs_float(swap, curve_set)


def fra_fair_rate(fra: FRA, curve_set: CurveSet) -> float:
    """The rate that makes the FRA worth zero, from the projection curve.

    Args:
        fra: The contract.
        curve_set: Discount and projection curves.

    Returns:
        The fair rate as a decimal on the FRA's day count.
    """
    return curve_set.projection.forward(fra.start, fra.end, day_count=fra.day_count)


@project.register
def _project_fra(fra: FRA, curve_set: CurveSet) -> tuple[Cashflow, ...]:
    sign = -fixed_leg_sign(fra.side)
    projected = fra_fair_rate(fra, curve_set)
    tau = fra.year_fraction
    return (
        Cashflow(
            payment_date=fra.end,
            amount=sign * fra.notional * (projected - fra.rate) * tau,
            leg="float",
            accrual_start=fra.start,
            accrual_end=fra.end,
            year_fraction=tau,
            rate=projected,
            currency=fra.currency,
        ),
    )
