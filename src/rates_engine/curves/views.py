"""The four views of one curve, and the conversions between them.

Discount, zero, par and forward are four ways of saying the same thing, and the
no-arbitrage relations between them are the CFA Level I and II term-structure
material. They are worth exporting for the same reason they are worth teaching:
each one answers a question the others answer awkwardly, and the conversions
are where a convention gets dropped.

Which is why a :class:`CurveView` never carries bare numbers. Compounding basis
and day count travel with the rates, because "the two-year rate is 4%" is not a
statement until you know which of the four it is and on what basis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_engine.conventions.calendar import SIFMA_US, BusinessDayConvention, SIFMAUSCalendar
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.schedule import Schedule
from rates_engine.curves.discount import CURVE_TIME_BASIS, DiscountCurve
from rates_engine.evidence import Evidence
from rates_engine.results import EngineResult

__all__ = ["CurveView", "CurveViews", "zero_curve", "par_curve", "forward_curve", "all_views"]


def _shift_months(day: date, months: int) -> date:
    """Shift by whole months, clamping to the last valid day of the target month.

    Only the forward view needs this, to name the end of a rolling tenor.
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, candidate)
        except ValueError:
            continue
    raise AssertionError("unreachable: day 28 exists in every month")  # pragma: no cover


@dataclass(frozen=True)
class CurveView:
    """Rates at a set of dates, with the conventions that make them mean something.

    Attributes:
        kind: ``"discount"``, ``"zero"``, ``"par"`` or ``"forward"``.
        dates: The maturity or period-end date of each point.
        values: Discount factors for ``"discount"``, decimal rates otherwise.
        compounding: ``"continuous"``, ``"annual"``, ``"simple"`` or ``None``
            for discount factors, which have no compounding basis.
        day_count: Basis the rates are quoted on, ``None`` for discount factors.
        frequency_months: Coupon frequency for a par curve, else ``None``.
        tenor_months: Forward period length for a forward curve, else ``None``.
    """

    kind: str
    dates: tuple[date, ...]
    values: tuple[float, ...]
    compounding: str | None
    day_count: DayCount | None
    frequency_months: int | None = None
    tenor_months: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise, with every convention present and ``None`` where not applicable."""
        return {
            "kind": self.kind,
            "dates": [d.isoformat() for d in self.dates],
            "values": list(self.values),
            "compounding": self.compounding,
            "day_count": self.day_count.value if self.day_count else None,
            "frequency_months": self.frequency_months,
            "tenor_months": self.tenor_months,
        }


@dataclass(frozen=True)
class CurveViews(EngineResult):
    """All four views of one curve, exported together.

    Attributes:
        discount: Discount factors at the node dates.
        zero: Zero-coupon rates.
        par: Par swap rates.
        forward: Forward rates.
    """

    discount: CurveView
    zero: CurveView
    par: CurveView
    forward: CurveView

    def payload_fields(self) -> dict[str, Any]:
        """The four views, each with its own conventions."""
        return {
            "discount": self.discount.to_dict(),
            "zero": self.zero.to_dict(),
            "par": self.par.to_dict(),
            "forward": self.forward.to_dict(),
        }


def zero_curve(
    curve: DiscountCurve,
    dates: tuple[date, ...] | None = None,
    *,
    compounding: str = "continuous",
    day_count: DayCount = CURVE_TIME_BASIS,
) -> CurveView:
    """Zero-coupon rates implied by a discount curve.

    Args:
        curve: The discount curve.
        dates: Maturities to report. Defaults to the curve's own nodes.
        compounding: ``"continuous"``, ``"annual"`` or ``"simple"``.
        day_count: Basis for the rates.

    Returns:
        A :class:`CurveView` of kind ``"zero"``.

    Raises:
        UnsupportedConventionError: ``compounding`` is not implemented.
    """
    points = dates or curve.nodes
    return CurveView(
        kind="zero",
        dates=tuple(points),
        values=tuple(
            curve.zero(d, compounding=compounding, day_count=day_count) for d in points
        ),
        compounding=compounding,
        day_count=day_count,
    )


def par_curve(
    curve: DiscountCurve,
    dates: tuple[date, ...] | None = None,
    *,
    frequency_months: int = 12,
    day_count: DayCount = DayCount.ACT_360,
    start: date | None = None,
    calendar: SIFMAUSCalendar = SIFMA_US,
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
    payment_lag_days: int = 0,
) -> CurveView:
    """Par swap rates: the fixed rate that prices a swap to zero at each maturity.

    Built on :class:`~rates_engine.conventions.schedule.Schedule`, the same
    generator the instruments use. Two schedule generators in one package is
    one too many: they drift on business-day rolls, and the symptom is a par
    curve that disagrees with a swap struck at its own par rate by a fraction
    of a basis point — small enough to look like rounding and large enough to
    fail a round-trip test.

    Args:
        curve: The discount curve.
        dates: Maturities to report. Defaults to the curve's own nodes.
        frequency_months: Fixed leg frequency in months.
        day_count: Fixed leg accrual basis.
        start: Swap start date. Defaults to the curve's valuation date.
        calendar: Calendar for payment rolls.
        convention: Roll rule for payment dates.
        payment_lag_days: Business days from accrual end to payment.

    Returns:
        A :class:`CurveView` of kind ``"par"``.

    Raises:
        ValueError: A requested maturity is not after the start date.
    """
    points = dates or curve.nodes
    begin = start or curve.as_of
    values: list[float] = []
    for maturity in points:
        if maturity <= begin:
            raise ValueError(f"par rate needs a maturity after {begin}, got {maturity}")
        schedule = Schedule.generate(
            begin,
            maturity,
            frequency_months=frequency_months,
            calendar=calendar,
            convention=convention,
            payment_lag_days=payment_lag_days,
        )
        annuity = sum(
            year_fraction(accrual_start, accrual_end, day_count) * curve.df(payment)
            for accrual_start, accrual_end, payment in zip(
                schedule.accrual_start, schedule.accrual_end, schedule.payment
            , strict=True)
        )
        floating = sum(
            (curve.df(accrual_start) / curve.df(accrual_end) - 1.0) * curve.df(payment)
            for accrual_start, accrual_end, payment in zip(
                schedule.accrual_start, schedule.accrual_end, schedule.payment
            , strict=True)
        )
        values.append(floating / annuity)
    return CurveView(
        kind="par",
        dates=tuple(points),
        values=tuple(values),
        compounding="simple",
        day_count=day_count,
        frequency_months=frequency_months,
    )


def forward_curve(
    curve: DiscountCurve,
    dates: tuple[date, ...] | None = None,
    *,
    tenor_months: int = 3,
    day_count: DayCount = DayCount.ACT_360,
    compounding: str = "simple",
) -> CurveView:
    """Forward rates over a rolling tenor starting at each date.

    Args:
        curve: The discount curve.
        dates: Period start dates. Defaults to the curve's own nodes.
        tenor_months: Forward period length in months.
        day_count: Basis for the period's year fraction.
        compounding: ``"simple"``, ``"annual"`` or ``"continuous"``.

    Returns:
        A :class:`CurveView` of kind ``"forward"``, dated by period *start*.

    Raises:
        UnsupportedConventionError: ``compounding`` is not implemented.
    """
    points = dates or curve.nodes
    return CurveView(
        kind="forward",
        dates=tuple(points),
        values=tuple(
            curve.forward(
                d,
                Schedule.generate(d, _shift_months(d, tenor_months), frequency_months=tenor_months)
                .accrual_end[-1],
                day_count=day_count,
                compounding=compounding,
            )
            for d in points
        ),
        compounding=compounding,
        day_count=day_count,
        tenor_months=tenor_months,
    )


def all_views(
    curve: DiscountCurve,
    dates: tuple[date, ...] | None = None,
    *,
    compounding: str = "continuous",
    zero_day_count: DayCount = CURVE_TIME_BASIS,
    par_frequency_months: int = 12,
    par_day_count: DayCount = DayCount.ACT_360,
    forward_tenor_months: int = 3,
    source_evidence: Evidence | None = None,
) -> CurveViews:
    """Export all four views of a curve at once.

    Args:
        curve: The discount curve.
        dates: Dates to report on. Defaults to the curve's own nodes.
        compounding: Compounding for the zero view.
        zero_day_count: Day count for the zero view.
        par_frequency_months: Fixed leg frequency for the par view.
        par_day_count: Fixed leg basis for the par view.
        forward_tenor_months: Forward tenor in months.
        source_evidence: Evidence of the curve being viewed, chained in so a
            proxied curve is still visibly proxied after conversion.

    Returns:
        The :class:`CurveViews`.
    """
    points = tuple(dates or curve.nodes)
    evidence = Evidence(
        produced_by="curves.all_views",
        fields={
            "interpolation": curve.interpolation,
            "dates": [d.isoformat() for d in points],
            "zero_compounding": compounding,
            "zero_day_count": zero_day_count.value,
            "par_frequency_months": par_frequency_months,
            "par_day_count": par_day_count.value,
            "forward_tenor_months": forward_tenor_months,
        },
        sources=(source_evidence,) if source_evidence else (),
    )
    return CurveViews(
        evidence=evidence,
        discount=CurveView(
            kind="discount",
            dates=points,
            values=tuple(curve.df(d) for d in points),
            compounding=None,
            day_count=None,
        ),
        zero=zero_curve(curve, points, compounding=compounding, day_count=zero_day_count),
        par=par_curve(
            curve, points, frequency_months=par_frequency_months, day_count=par_day_count
        ),
        forward=forward_curve(curve, points, tenor_months=forward_tenor_months),
    )
