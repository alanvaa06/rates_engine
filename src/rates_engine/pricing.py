"""Present value, par rate, annuity and the parallel DV01.

Everything here discounts on ``curve_set.discount`` and nothing here decides
what that curve is. Collateralised flows belong on the OIS-SOFR curve because
the collateral earns SOFR, and the evidence of every result says so rather
than leaving it to be inferred from the absence of an alternative.

**DV01 is a central difference.** ``(PV(y - 1bp) - PV(y + 1bp)) / 2``, not a
one-sided bump. The symmetric form cancels the second-order term exactly,
which is what lets a receiver and a payer agree in magnitude to machine
precision and what lets the key-rate profile in :mod:`rates_engine.risk` sum
back to this number. A one-sided bump would leave a curvature residual in both
places, and it would look like a bug in the key rates rather than in the
differencing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from rates_engine.curves.discount import CurveSet
from rates_engine.evidence import Evidence
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.instruments.swaps import Side
from rates_engine.results import EngineResult

__all__ = ["Priceable", "Swappable", "PriceResult", "pv", "par_rate", "annuity", "dv01", "BUMP_BP"]

BUMP_BP = 1.0
"""Default bump size in basis points for :func:`dv01` and the risk measures."""


@runtime_checkable
class Priceable(Protocol):
    """Anything with dated cashflows that depend on a curve set."""

    @property
    def span(self) -> tuple[date, date]:
        """First and last date the instrument touches, as a half-open interval."""
        ...

    def cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The instrument's cashflows under ``curve_set``, signed for its side."""
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
        """Notional in USD."""
        ...

    @property
    def side(self) -> str:
        """``"payer"`` or ``"receiver"``."""
        ...

    def with_terms(self, **changes: object) -> Swappable:
        """A copy with some terms changed."""
        ...

    def fixed_cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The fixed leg alone."""
        ...

    def float_cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The floating leg alone."""
        ...


@dataclass(frozen=True)
class PriceResult(EngineResult):
    """One valuation number, its unit, and what produced it.

    Attributes:
        value: The number.
        measure: ``"pv"``, ``"par_rate"``, ``"annuity"`` or ``"dv01"``.
        unit: ``"USD"``, ``"decimal_rate"``, ``"years"`` or ``"USD_per_bp"``.
            Carried rather than implied, because a rate without a unit and a
            DV01 quoted per percent are the two classic ways to be off by a
            factor of ten thousand.
        cashflows: The flows behind a ``"pv"``, or ``None``.
    """

    value: float
    measure: str
    unit: str
    cashflows: tuple[Cashflow, ...] | None = None

    def payload_fields(self) -> dict[str, Any]:
        """The value, its measure and unit, and the cashflows if there are any."""
        return {
            "value": self.value,
            "measure": self.measure,
            "unit": self.unit,
            "cashflows": (
                [c.to_dict() for c in self.cashflows] if self.cashflows is not None else None
            ),
        }


def _discount_note() -> dict[str, Any]:
    return {
        "discounting": "collateral_rate_ois_sofr",
        "discounting_note": (
            "Discounted on the OIS-SOFR curve because collateral is remunerated at "
            "SOFR (Fujii-Shimada-Takahashi; Piterbarg), not on a separate funding curve."
        ),
    }


def _pv_of(flows: tuple[Cashflow, ...], curve_set: CurveSet) -> float:
    return sum(flow.amount * curve_set.discount.df(flow.payment_date) for flow in flows)


def _evidence(
    produced_by: str,
    instrument: Priceable,
    curve_set: CurveSet,
    extra: dict[str, Any],
    sources: tuple[Evidence, ...],
) -> Evidence:
    return Evidence(
        produced_by=produced_by,
        fields={
            "instrument": instrument.describe(),
            "curve_interpolation": curve_set.discount.interpolation,
            "dual_curve": curve_set.is_dual,
            "projection_curve": "tenor" if curve_set.is_dual else "discount",
            **_discount_note(),
            **extra,
        },
        sources=sources,
    )


def pv(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    source_evidence: tuple[Evidence, ...] = (),
) -> PriceResult:
    """Present value in USD, signed from the holder's point of view.

    Args:
        instrument: Anything with :meth:`cashflows`.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves, chained in so that a proxied
            curve stays visibly proxied in the price.

    Returns:
        A :class:`PriceResult` with ``measure="pv"`` and ``unit="USD"``.
    """
    flows = instrument.cashflows(curve_set)
    value = _pv_of(flows, curve_set)
    return PriceResult(
        evidence=_evidence("pricing.pv", instrument, curve_set, {"cashflows": len(flows)}, source_evidence),
        value=value,
        measure="pv",
        unit="USD",
        cashflows=flows,
    )


def annuity(
    swap: Swappable,
    curve_set: CurveSet,
    *,
    source_evidence: tuple[Evidence, ...] = (),
) -> PriceResult:
    """Fixed-leg annuity per unit notional, in years of discounted accrual.

    Args:
        swap: A two-legged instrument.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves.

    Returns:
        A :class:`PriceResult` with ``measure="annuity"`` and ``unit="years"``.
    """
    unit_swap = swap.with_terms(fixed_rate=1.0, side=Side.RECEIVER)
    value = _pv_of(unit_swap.fixed_cashflows(curve_set), curve_set) / swap.notional
    return PriceResult(
        evidence=_evidence("pricing.annuity", swap, curve_set, {}, source_evidence),
        value=value,
        measure="annuity",
        unit="years",
    )


def par_rate(
    swap: Swappable,
    curve_set: CurveSet,
    *,
    source_evidence: tuple[Evidence, ...] = (),
) -> PriceResult:
    """The fixed rate that prices the swap at zero, as a decimal.

    Computed as the floating leg's present value over the annuity, both taken
    on the same curve set, so the two-curve case falls out without a branch:
    the floating leg finds its forwards on the projection curve and both legs
    discount on the OIS curve.

    Args:
        swap: A two-legged instrument.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves.

    Returns:
        A :class:`PriceResult` with ``measure="par_rate"`` and
        ``unit="decimal_rate"``.

    Raises:
        ZeroDivisionError: The annuity is zero, which means the fixed leg has
            no periods left to discount.
    """
    payer = swap.with_terms(side=Side.PAYER)
    float_pv = _pv_of(payer.float_cashflows(curve_set), curve_set)
    annuity_value = annuity(swap, curve_set).value
    value = float_pv / (swap.notional * annuity_value)
    return PriceResult(
        evidence=_evidence(
            "pricing.par_rate",
            swap,
            curve_set,
            {"annuity_years": annuity_value, "float_leg_pv": float_pv},
            source_evidence,
        ),
        value=value,
        measure="par_rate",
        unit="decimal_rate",
    )


def dv01(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
) -> PriceResult:
    """Parallel DV01 in USD per basis point, by symmetric bump and full reprice.

    The whole zero curve — discount and projection alike — shifts by
    ``bump_bp``, and the instrument is repriced from scratch. Positive for a
    receiver, negative for a payer, because the sign follows the value of the
    position rather than the direction of the shift.

    Args:
        instrument: Anything with :meth:`cashflows`.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.

    Returns:
        A :class:`PriceResult` with ``measure="dv01"`` and
        ``unit="USD_per_bp"``.

    Raises:
        ValueError: ``bump_bp`` is not positive.
    """
    if bump_bp <= 0.0:
        raise ValueError(f"bump_bp must be positive, got {bump_bp!r}")
    shift = bump_bp * 1e-4
    up = _pv_of(instrument.cashflows(curve_set.shifted(shift)), curve_set.shifted(shift))
    down = _pv_of(instrument.cashflows(curve_set.shifted(-shift)), curve_set.shifted(-shift))
    value = (down - up) / 2.0 / bump_bp
    return PriceResult(
        evidence=_evidence(
            "pricing.dv01",
            instrument,
            curve_set,
            {
                "bump_bp": bump_bp,
                "bump_basis": "zero_curve_parallel",
                "bump_shape": "parallel",
                "difference": "central",
            },
            source_evidence,
        ),
        value=value,
        measure="dv01",
        unit="USD_per_bp",
    )
