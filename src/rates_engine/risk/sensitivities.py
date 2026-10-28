"""Key-rate risk, duration conventions, and the measures that refuse to exist.

Two families live here, and the reason they share a module is that they share
one trap: a risk number is only meaningful with its convention attached, and
the conventions are where the mistakes are.

**Key rate, and why the shocks are tents.** A key-rate profile is only additive
if the individual shocks add up to the parallel shift. Bump each node
independently by a basis point and they do not: the shocks overlap nowhere and
leave the space between nodes unmoved. So the shocks here are triangular, with
a peak of one basis point at their own key tenor falling linearly to zero at
the neighbouring ones, flat outside the first and last. That makes them a
partition of unity, and :func:`~rates_engine.risk.bumps.tent_weights` is tested directly on that
property rather than only through the sum of the key rates it produces.

**What a key-rate profile is not.** It is not a property of the swap alone. It
depends on where the curve's nodes are and how the curve interpolates between
them, so two curves that price every instrument identically can report
different profiles. That is recorded in the evidence of every result here, as
a field rather than a docstring, because someone comparing two profiles needs
to see it at the point of comparison.

**Duration, and the ones that do not exist.** A par swap is worth zero, so
every price-normalised measure divides by zero. Modified duration, effective
duration and key-rate duration are all undefined there, and this module raises
rather than returning an enormous number. The measures that *are* defined at
zero price are the monetary ones — DV01, money duration, money convexity — and
those are what the shock table reconciles against. Macaulay and modified
duration need a single yield to maturity and therefore an instrument priced
away from zero; they are stubs that say so and point at ``FixedRateBond`` in
v1.1. Neither is ever served as effective duration under another name.

**A trade that has started.** Every measure takes ``fixings``, the published
fixings ``pv`` takes, and passes them to each reprice: the history of a
coupon already decided does not move with the curve, only its forecast and
its discounting do. Their record, and any forecast standing in for a fixing
dated ``as_of``, is chained into the result's evidence once. It is the same
on every moved curve, so it is taken from the first reprice the measure
makes anyway (:func:`~rates_engine.risk.bumps.repriced_with_history`), not
from a projection of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, NoReturn

from rates_engine.conventions.daycount import CURVE_TIME_BASIS, year_fraction
from rates_engine.core.errors import KeyTenorOutOfRangeError, UndefinedDurationError
from rates_engine.core.evidence import Evidence
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import CurveSet
from rates_engine.market.snapshot import MarketSnapshot
from rates_engine.pricing.collateral import valuation_warnings
from rates_engine.pricing.linear import (
    PriceResult,
    discounted_value_with_history,
    valuation_evidence,
)
from rates_engine.pricing.projection import Priceable
from rates_engine.risk.bumps import (
    BUMP_BP,
    repriced,
    repriced_with_history,
    shift_from_bp,
    tent_shift,
)

__all__ = [
    "RiskResult",
    "KeyRateResult",
    "dv01",
    "key_rate_dv01",
    "key_rate_duration",
    "pvbp",
    "money_duration",
    "money_convexity",
    "effective_duration",
    "effective_convexity",
    "macaulay_duration",
    "modified_duration",
    "ZERO_PRICE_TOLERANCE",
    "INTERPOLATION_CAVEAT",
]

ZERO_PRICE_TOLERANCE = 1e-8
"""Fraction of notional below which a price counts as zero for normalisation.

A par swap prices to a rounding error, not to exactly zero, so the test for
"is this normalisable" has to be a tolerance. Relative to notional rather than
absolute, so it means the same thing on a one million and a one billion trade.
"""

INTERPOLATION_CAVEAT = (
    "A key-rate profile depends on where the curve's nodes sit and how it "
    "interpolates between them. Two curves that reprice every input instrument "
    "identically can report different profiles, so profiles are only comparable "
    "across curves built the same way."
)


@dataclass(frozen=True)
class RiskResult(EngineResult):
    """One risk number with its unit and the bump that produced it.

    Attributes:
        value: The number.
        measure: Which measure, e.g. ``"money_convexity"``.
        unit: Its unit, e.g. ``"<CCY>_per_bp"``, where the currency is the
            discount curve's rather than a literal.
        bump_bp: Bump size used, or ``None`` for measures that need none.
    """

    value: float
    measure: str
    unit: str
    bump_bp: float | None

    def payload_fields(self) -> dict[str, Any]:
        """The value, the measure, its unit and the bump."""
        return {
            "value": self.value,
            "measure": self.measure,
            "unit": self.unit,
            "bump_bp": self.bump_bp,
        }


@dataclass(frozen=True)
class KeyRateResult(EngineResult):
    """A key-rate profile, keyed by tenor in years.

    Attributes:
        values: Key tenor in years to the risk at that tenor.
        measure: ``"key_rate_dv01"`` or ``"key_rate_duration"``.
        unit: ``"<CCY>_per_bp"`` for the DV01 profile, ``"years"`` for the
            duration profile.
        key_tenors: The tenors, in the order requested.
        total: Sum across tenors. Equals the parallel measure in the same
            unit -- :func:`dv01` for the first, :func:`effective_duration`
            for the second -- which is the point of the tent shocks.
        bump_bp: Bump size used, in basis points.
    """

    values: dict[float, float]
    measure: str
    unit: str
    key_tenors: tuple[float, ...]
    total: float
    bump_bp: float

    def payload_fields(self) -> dict[str, Any]:
        """The profile, its total and the conventions behind it."""
        return {
            "measure": self.measure,
            "unit": self.unit,
            "key_tenors": list(self.key_tenors),
            "values": {str(k): v for k, v in self.values.items()},
            "total": self.total,
            "bump_bp": self.bump_bp,
            "bump_basis": "zero_curve_node",
            "bump_shape": "tent",
        }


def _tenor_years(as_of: date, tenor: float | date) -> float:
    return (
        year_fraction(as_of, tenor, CURVE_TIME_BASIS) if isinstance(tenor, date) else float(tenor)
    )


def _normalise_tenors(curve_set: CurveSet, key_tenors: tuple[float | date, ...]) -> tuple[float, ...]:
    """Convert key tenors to years and refuse any that fall outside the curve."""
    as_of = curve_set.as_of
    years = tuple(_tenor_years(as_of, t) for t in key_tenors)
    node_times = curve_set.discount.node_times
    span = (min(node_times), max(node_times))
    for original, value in zip(key_tenors, years, strict=True):
        if value <= 0.0 or value > span[1] + 1e-12:
            raise KeyTenorOutOfRangeError(
                f"key tenor {original!r} is {value:.6f} years, outside the curve span "
                f"{span[0]:.6f} to {span[1]:.6f} years; key rates are not extrapolated"
            )
    return years


def _with_history(
    source_evidence: tuple[Evidence, ...], history: Evidence | None
) -> tuple[Evidence, ...]:
    """``source_evidence`` and, when the trade has a decided coupon, the record of it."""
    return source_evidence if history is None else (*source_evidence, history)


def dv01(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
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
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        A :class:`PriceResult` with ``measure="dv01"`` and
        ``unit="<CCY>_per_bp"`` for the discount curve's currency.

    Raises:
        ValueError: ``bump_bp`` is not positive.
        MissingFixingError: A floating coupon is already decided and
            ``fixings`` does not cover it.
    """
    shift = shift_from_bp(bump_bp)
    up, history = repriced_with_history(instrument, curve_set, shift, fixings=fixings)
    down = repriced(instrument, curve_set, -shift, fixings=fixings)
    value = (down - up) / 2.0 / bump_bp
    return PriceResult(
        evidence=valuation_evidence(
            "pricing.dv01",
            instrument,
            curve_set,
            {
                "bump_bp": bump_bp,
                "bump_basis": "zero_curve_parallel",
                "bump_shape": "parallel",
                "difference": "central",
            },
            _with_history(source_evidence, history),
        ),
        value=value,
        measure="dv01",
        unit=f"{curve_set.discount.currency.value}_per_bp",
    )


def key_rate_dv01(
    instrument: Priceable,
    curve_set: CurveSet,
    key_tenors: tuple[float | date, ...],
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> KeyRateResult:
    """The instrument's DV01 attributed along the curve, in USD per basis point.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        key_tenors: Key tenors, in years or as dates.
        bump_bp: Peak height of each tent shock, in basis points.
        source_evidence: Evidence of the curves, chained in.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        The :class:`KeyRateResult`. Its ``total`` equals the parallel DV01
        because the tent shocks sum to a parallel shift.

    Raises:
        KeyTenorOutOfRangeError: A key tenor falls outside the curve's span.
        ValueError: ``bump_bp`` is not positive, or the tenors are not
            strictly increasing.
    """
    shift = shift_from_bp(bump_bp)
    years = _normalise_tenors(curve_set, key_tenors)
    values: dict[float, float] = {}
    history: Evidence | None = None
    for index, tenor in enumerate(years):
        up_shift = tent_shift(index, years, shift)
        if index == 0:
            up, history = repriced_with_history(instrument, curve_set, up_shift, fixings=fixings)
        else:
            up = repriced(instrument, curve_set, up_shift, fixings=fixings)
        down = repriced(instrument, curve_set, tent_shift(index, years, -shift), fixings=fixings)
        values[tenor] = (down - up) / 2.0 / bump_bp

    total = sum(values.values())
    evidence = Evidence(
        produced_by="risk.key_rate_dv01",
        fields={
            "instrument": instrument.describe(),
            "bump_basis": "zero_curve_node",
            "bump_shape": "tent",
            "bump_bp": bump_bp,
            "difference": "central",
            "key_tenors_years": list(years),
            "curve_nodes": [n.isoformat() for n in curve_set.discount.nodes],
            "curve_node_times_years": list(curve_set.discount.node_times),
            "interpolation": curve_set.discount.interpolation,
            "interpolation_caveat": INTERPOLATION_CAVEAT,
            "partition_of_unity": True,
        },
        sources=_with_history(source_evidence, history),
        warnings=valuation_warnings(instrument, curve_set),
    )
    return KeyRateResult(
        evidence=evidence,
        values=values,
        measure="key_rate_dv01",
        unit=f"{curve_set.discount.currency.value}_per_bp",
        key_tenors=years,
        total=total,
        bump_bp=bump_bp,
    )


def _require_normalisable(
    instrument: Priceable,
    curve_set: CurveSet,
    measure: str,
    fixings: MarketSnapshot | None = None,
) -> tuple[float, Evidence | None]:
    """Return the price and the record of its history, or refuse: normalising by it is undefined."""
    price, history = discounted_value_with_history(instrument, curve_set, fixings=fixings)
    notional = float(getattr(instrument, "notional", 1.0)) or 1.0
    if abs(price) <= ZERO_PRICE_TOLERANCE * abs(notional):
        raise UndefinedDurationError(
            f"{measure} normalises by price, and this instrument prices at {price!r} "
            f"(within {ZERO_PRICE_TOLERANCE} of zero relative to a notional of "
            f"{notional:,.0f}). A par swap has no duration in that sense. The defined "
            "measures here are dv01, key_rate_dv01 and money_convexity."
        )
    return price, history


def key_rate_duration(
    instrument: Priceable,
    curve_set: CurveSet,
    key_tenors: tuple[float | date, ...],
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> KeyRateResult:
    """The key-rate profile normalised by price, in years: ``KR DV01 / (PV x 1bp)``.

    The decomposition of :func:`effective_duration` along the curve, in its
    unit, so the profile sums to it. PRD-001 AC-10.5 defines it this way.
    It used to divide by the price alone, which left the profile in
    "per bp" and exactly 1e4 smaller than the duration it decomposes.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        key_tenors: Key tenors, in years or as dates.
        bump_bp: Peak height of each tent shock, in basis points.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        The :class:`KeyRateResult` with ``measure="key_rate_duration"`` and
        ``unit="years"``; its ``total`` is the effective duration.

    Raises:
        UndefinedDurationError: The instrument prices at zero, as a par swap
            does. Use :func:`key_rate_dv01` there.
        KeyTenorOutOfRangeError: A key tenor falls outside the curve's span.
    """
    price, _ = _require_normalisable(instrument, curve_set, "key_rate_duration", fixings)
    raw = key_rate_dv01(
        instrument,
        curve_set,
        key_tenors,
        bump_bp=bump_bp,
        source_evidence=source_evidence,
        fixings=fixings,
    )
    one_bp = shift_from_bp(1.0)
    scaled = {tenor: value / (price * one_bp) for tenor, value in raw.values.items()}
    evidence = Evidence(
        produced_by="risk.key_rate_duration",
        fields={
            **dict(raw.evidence.fields),
            "price": price,
            "normalised_by": "pv_times_one_bp",
            "derivation": "key_rate_dv01 / (pv * 1e-4)",
        },
        sources=raw.evidence.sources,
        warnings=raw.evidence.warnings,
    )
    return KeyRateResult(
        evidence=evidence,
        values=scaled,
        measure="key_rate_duration",
        unit="years",
        key_tenors=raw.key_tenors,
        total=sum(scaled.values()),
        bump_bp=bump_bp,
    )


def pvbp(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> RiskResult:
    """Price value of a basis point, in USD per basis point.

    The same number as :func:`~rates_engine.risk.sensitivities.dv01`, and computed by
    calling it rather than reimplemented — the two names exist because both
    are in common use, not because they are different quantities.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        A :class:`RiskResult` whose unit carries the curve's currency.
    """
    base = dv01(
        instrument, curve_set, bump_bp=bump_bp, source_evidence=source_evidence, fixings=fixings
    )
    return RiskResult(
        evidence=Evidence(
            produced_by="risk.pvbp",
            fields={"equivalent_to": "pricing.dv01"},
            sources=(base.evidence,),
        ),
        value=base.value,
        measure="pvbp",
        unit=f"{curve_set.discount.currency.value}_per_bp",
        bump_bp=bump_bp,
    )


def money_duration(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> RiskResult:
    """Money duration: the derivative of price with respect to yield, in USD.

    ``DV01 x 10,000``. The unit is USD per unit of yield, not per basis point,
    and the two differ by exactly that factor — which is the most common way
    to be wrong by four orders of magnitude, hence the explicit unit on the
    result.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        A :class:`RiskResult` whose unit carries the curve's currency.
    """
    base = dv01(
        instrument, curve_set, bump_bp=bump_bp, source_evidence=source_evidence, fixings=fixings
    )
    return RiskResult(
        evidence=Evidence(
            produced_by="risk.money_duration",
            fields={"derivation": "dv01 * 10_000", "dv01": base.value},
            sources=(base.evidence,),
        ),
        value=base.value * 1e4,
        measure="money_duration",
        unit=f"{curve_set.discount.currency.value}_per_unit_yield",
        bump_bp=bump_bp,
    )


def money_convexity(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> RiskResult:
    """Money convexity: the second derivative of price with respect to yield.

    Defined even when the price is zero, which is the whole reason it exists
    alongside :func:`effective_convexity`. A hedged swap position prices at
    zero and has no normalised convexity, but it certainly has curvature, and
    ``0.5 * money_convexity * dy^2`` is what predicts the residual in the shock
    table.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        A :class:`RiskResult` whose unit carries the curve's currency.

    Raises:
        ValueError: ``bump_bp`` is not positive.
        MissingFixingError: A floating coupon is already decided and
            ``fixings`` does not cover it.
    """
    shift = shift_from_bp(bump_bp)
    base, history = discounted_value_with_history(instrument, curve_set, fixings=fixings)
    up = repriced(instrument, curve_set, shift, fixings=fixings)
    down = repriced(instrument, curve_set, -shift, fixings=fixings)
    value = (up + down - 2.0 * base) / (shift * shift)
    return RiskResult(
        evidence=Evidence(
            produced_by="risk.money_convexity",
            fields={
                "instrument": instrument.describe(),
                "bump_bp": bump_bp,
                "bump_basis": "zero_curve_parallel",
                "difference": "central_second",
                "base_pv": base,
                "defined_at_zero_price": True,
            },
            sources=_with_history(source_evidence, history),
            warnings=valuation_warnings(instrument, curve_set),
        ),
        value=value,
        measure="money_convexity",
        unit=f"{curve_set.discount.currency.value}_per_yield_squared",
        bump_bp=bump_bp,
    )


def effective_duration(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> RiskResult:
    """Effective duration: ``(PV- - PV+) / (2 * PV0 * dy)``, in years.

    *Effective* because the shift is applied to the curve and the instrument
    is repriced in full, not derived from a single yield to maturity. That
    distinction is the reason this and :func:`modified_duration` are different
    functions rather than aliases.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        A :class:`RiskResult` with ``unit="years"``.

    Raises:
        UndefinedDurationError: The instrument prices at zero.
        ValueError: ``bump_bp`` is not positive.
    """
    shift = shift_from_bp(bump_bp)
    price, history = _require_normalisable(instrument, curve_set, "effective_duration", fixings)
    up = repriced(instrument, curve_set, shift, fixings=fixings)
    down = repriced(instrument, curve_set, -shift, fixings=fixings)
    value = (down - up) / (2.0 * price * shift)
    return RiskResult(
        evidence=Evidence(
            produced_by="risk.effective_duration",
            fields={
                "instrument": instrument.describe(),
                "bump_bp": bump_bp,
                "bump_basis": "zero_curve_parallel",
                "difference": "central",
                "base_pv": price,
                "kind": "effective",
                "kind_note": (
                    "Derived from a curve shift and a full reprice, not from a single "
                    "yield to maturity. Not modified duration."
                ),
            },
            sources=_with_history(source_evidence, history),
            warnings=valuation_warnings(instrument, curve_set),
        ),
        value=value,
        measure="effective_duration",
        unit="years",
        bump_bp=bump_bp,
    )


def effective_convexity(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> RiskResult:
    """Effective convexity: ``(PV+ + PV- - 2 PV0) / (PV0 * dy^2)``, in years squared.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided, as
            for :func:`~rates_engine.pricing.linear.pv`.

    Returns:
        A :class:`RiskResult` with ``unit="years_squared"``.

    Raises:
        UndefinedDurationError: The instrument prices at zero. Use
            :func:`money_convexity` there.
        ValueError: ``bump_bp`` is not positive.
    """
    price, _ = _require_normalisable(instrument, curve_set, "effective_convexity", fixings)
    money = money_convexity(
        instrument, curve_set, bump_bp=bump_bp, source_evidence=source_evidence, fixings=fixings
    )
    return RiskResult(
        evidence=Evidence(
            produced_by="risk.effective_convexity",
            fields={"base_pv": price, "normalised_by": "pv", "kind": "effective"},
            sources=(money.evidence,),
        ),
        value=money.value / price,
        measure="effective_convexity",
        unit="years_squared",
        bump_bp=bump_bp,
    )


_YIELD_STUB = (
    "{name} is defined from a single yield to maturity, which needs an instrument "
    "priced away from zero — a fixed rate bond. v1 prices swaps, futures and FRAs, "
    "so there is nothing here to compute it on; FixedRateBond arrives in v1.1. "
    "effective_duration is a curve-based measure and is NOT the same quantity, so "
    "it is not served under this name. For a par swap the defined measures are "
    "dv01, key_rate_dv01 and money_convexity."
)


def macaulay_duration(*args: Any, **kwargs: Any) -> NoReturn:
    """Not implemented in v1: it needs a yield, and therefore a bond.

    Args:
        *args: Ignored.
        **kwargs: Ignored.

    Raises:
        NotImplementedError: Always. The stub exists so that an agent or a
            reader reaching for the name gets the reason rather than an
            ``AttributeError``.
    """
    del args, kwargs
    raise NotImplementedError(_YIELD_STUB.format(name="macaulay_duration"))


def modified_duration(*args: Any, **kwargs: Any) -> NoReturn:
    """Not implemented in v1: it needs a yield, and therefore a bond.

    Args:
        *args: Ignored.
        **kwargs: Ignored.

    Raises:
        NotImplementedError: Always. See :func:`macaulay_duration`.
    """
    del args, kwargs
    raise NotImplementedError(_YIELD_STUB.format(name="modified_duration"))
