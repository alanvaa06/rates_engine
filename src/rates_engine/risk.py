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
partition of unity, and :func:`tent_weights` is tested directly on that
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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, NoReturn

from rates_engine.conventions.daycount import year_fraction
from rates_engine.curves.discount import CURVE_TIME_BASIS, CurveSet
from rates_engine.errors import KeyTenorOutOfRangeError, UndefinedDurationError
from rates_engine.evidence import Evidence
from rates_engine.pricing import BUMP_BP, Priceable, dv01, pv
from rates_engine.results import EngineResult

__all__ = [
    "RiskResult",
    "KeyRateResult",
    "tent_weights",
    "key_rate_dv01",
    "key_rate_duration",
    "pvbp",
    "money_duration",
    "money_convexity",
    "effective_duration",
    "effective_convexity",
    "macaulay_duration",
    "modified_duration",
    "GreeksResult",
    "option_greeks",
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
        unit: ``"<CCY>_per_bp"`` or ``"per_bp"``.
        key_tenors: The tenors, in the order requested.
        total: Sum across tenors. Equals the parallel measure, which is the
            point of the tent shocks.
        bump_bp: Bump size used.
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


def tent_weights(time: float, key_tenors: tuple[float, ...]) -> tuple[float, ...]:
    """Weights of each key tenor's triangular shock at one point in time.

    The weights are a partition of unity: they are non-negative and sum to
    exactly one at every time, so the shocks add up to a parallel shift. Below
    the first key tenor the first shock carries all the weight, and above the
    last the last one does, which is what keeps the property true outside the
    grid as well as inside it.

    Args:
        time: Time in years from the valuation date.
        key_tenors: Key tenors in years, strictly increasing.

    Returns:
        One weight per key tenor, in the same order.

    Raises:
        ValueError: ``key_tenors`` is empty or not strictly increasing.
    """
    if not key_tenors:
        raise ValueError("key_tenors must not be empty")
    if any(b <= a for a, b in zip(key_tenors, key_tenors[1:], strict=False)):
        raise ValueError(f"key_tenors must be strictly increasing, got {key_tenors}")
    if len(key_tenors) == 1:
        return (1.0,)
    if time <= key_tenors[0]:
        return (1.0,) + (0.0,) * (len(key_tenors) - 1)
    if time >= key_tenors[-1]:
        return (0.0,) * (len(key_tenors) - 1) + (1.0,)
    weights = [0.0] * len(key_tenors)
    for index in range(len(key_tenors) - 1):
        left, right = key_tenors[index], key_tenors[index + 1]
        if left <= time <= right:
            share = (time - left) / (right - left)
            weights[index] = 1.0 - share
            weights[index + 1] = share
            break
    return tuple(weights)


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


def key_rate_dv01(
    instrument: Priceable,
    curve_set: CurveSet,
    key_tenors: tuple[float | date, ...],
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
) -> KeyRateResult:
    """The instrument's DV01 attributed along the curve, in USD per basis point.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        key_tenors: Key tenors, in years or as dates.
        bump_bp: Peak height of each tent shock, in basis points.
        source_evidence: Evidence of the curves, chained in.

    Returns:
        The :class:`KeyRateResult`. Its ``total`` equals the parallel DV01
        because the tent shocks sum to a parallel shift.

    Raises:
        KeyTenorOutOfRangeError: A key tenor falls outside the curve's span.
        ValueError: ``bump_bp`` is not positive, or the tenors are not
            strictly increasing.
    """
    if bump_bp <= 0.0:
        raise ValueError(f"bump_bp must be positive, got {bump_bp!r}")
    years = _normalise_tenors(curve_set, key_tenors)
    shift = bump_bp * 1e-4
    values: dict[float, float] = {}
    for index, tenor in enumerate(years):
        def shape(t: float, i: int = index) -> float:
            return shift * tent_weights(t, years)[i]

        def negative(t: float, i: int = index) -> float:
            return -shift * tent_weights(t, years)[i]

        up_set = curve_set.shifted(shape)
        down_set = curve_set.shifted(negative)
        up = pv(instrument, up_set).value
        down = pv(instrument, down_set).value
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
        sources=source_evidence,
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


def _require_normalisable(instrument: Priceable, curve_set: CurveSet, measure: str) -> float:
    """Return the price, or refuse because normalising by it is undefined."""
    price = pv(instrument, curve_set).value
    notional = float(getattr(instrument, "notional", 1.0)) or 1.0
    if abs(price) <= ZERO_PRICE_TOLERANCE * abs(notional):
        raise UndefinedDurationError(
            f"{measure} normalises by price, and this instrument prices at {price!r} "
            f"(within {ZERO_PRICE_TOLERANCE} of zero relative to a notional of "
            f"{notional:,.0f}). A par swap has no duration in that sense. The defined "
            "measures here are dv01, key_rate_dv01 and money_convexity."
        )
    return price


def key_rate_duration(
    instrument: Priceable,
    curve_set: CurveSet,
    key_tenors: tuple[float | date, ...],
    *,
    bump_bp: float = BUMP_BP,
    source_evidence: tuple[Evidence, ...] = (),
) -> KeyRateResult:
    """The key-rate profile normalised by price, per basis point.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        key_tenors: Key tenors, in years or as dates.
        bump_bp: Peak height of each tent shock, in basis points.
        source_evidence: Evidence of the curves.

    Returns:
        The :class:`KeyRateResult` with ``measure="key_rate_duration"``.

    Raises:
        UndefinedDurationError: The instrument prices at zero, as a par swap
            does. Use :func:`key_rate_dv01` there.
        KeyTenorOutOfRangeError: A key tenor falls outside the curve's span.
    """
    price = _require_normalisable(instrument, curve_set, "key_rate_duration")
    raw = key_rate_dv01(
        instrument, curve_set, key_tenors, bump_bp=bump_bp, source_evidence=source_evidence
    )
    scaled = {tenor: value / price for tenor, value in raw.values.items()}
    evidence = Evidence(
        produced_by="risk.key_rate_duration",
        fields={**dict(raw.evidence.fields), "price": price, "normalised_by": "pv"},
        sources=raw.evidence.sources,
    )
    return KeyRateResult(
        evidence=evidence,
        values=scaled,
        measure="key_rate_duration",
        unit="per_bp",
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
) -> RiskResult:
    """Price value of a basis point, in USD per basis point.

    The same number as :func:`~rates_engine.pricing.dv01`, and computed by
    calling it rather than reimplemented — the two names exist because both
    are in common use, not because they are different quantities.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.

    Returns:
        A :class:`RiskResult` whose unit carries the curve's currency.
    """
    base = dv01(instrument, curve_set, bump_bp=bump_bp, source_evidence=source_evidence)
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

    Returns:
        A :class:`RiskResult` whose unit carries the curve's currency.
    """
    base = dv01(instrument, curve_set, bump_bp=bump_bp, source_evidence=source_evidence)
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

    Returns:
        A :class:`RiskResult` whose unit carries the curve's currency.

    Raises:
        ValueError: ``bump_bp`` is not positive.
    """
    if bump_bp <= 0.0:
        raise ValueError(f"bump_bp must be positive, got {bump_bp!r}")
    shift = bump_bp * 1e-4
    base = pv(instrument, curve_set).value
    up = pv(instrument, curve_set.shifted(shift)).value
    down = pv(instrument, curve_set.shifted(-shift)).value
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
            sources=source_evidence,
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

    Returns:
        A :class:`RiskResult` with ``unit="years"``.

    Raises:
        UndefinedDurationError: The instrument prices at zero.
        ValueError: ``bump_bp`` is not positive.
    """
    if bump_bp <= 0.0:
        raise ValueError(f"bump_bp must be positive, got {bump_bp!r}")
    price = _require_normalisable(instrument, curve_set, "effective_duration")
    shift = bump_bp * 1e-4
    up = pv(instrument, curve_set.shifted(shift)).value
    down = pv(instrument, curve_set.shifted(-shift)).value
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
            sources=source_evidence,
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
) -> RiskResult:
    """Effective convexity: ``(PV+ + PV- - 2 PV0) / (PV0 * dy^2)``, in years squared.

    Args:
        instrument: Anything with cashflows.
        curve_set: Discount and projection curves.
        bump_bp: Shift size in basis points.
        source_evidence: Evidence of the curves.

    Returns:
        A :class:`RiskResult` with ``unit="years_squared"``.

    Raises:
        UndefinedDurationError: The instrument prices at zero. Use
            :func:`money_convexity` there.
        ValueError: ``bump_bp`` is not positive.
    """
    price = _require_normalisable(instrument, curve_set, "effective_convexity")
    money = money_convexity(
        instrument, curve_set, bump_bp=bump_bp, source_evidence=source_evidence
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


@dataclass(frozen=True)
class GreeksResult(EngineResult):
    """An option's sensitivities, every one of them a bumped reprice.

    Attributes:
        delta: Change in value for a one basis point fall in the curve, per
            basis point — the option's DV01, on the same sign convention as
            :func:`~rates_engine.pricing.dv01`.
        gamma: Change in :attr:`delta` per basis point squared.
        vega: Change in value per basis point of *normal* volatility. Reported on the normal basis whatever model
            priced the option, because that is what the market quotes and
            what two desks can compare.
        theta: Change in value for one calendar day passing, per day.
            Negative for a long option, which is the whole of what a long
            option costs to hold.
        value: The unbumped price.
        rate_bump_bp: Curve bump used, in basis points.
        vol_bump_bp: Volatility bump used, in basis points of normal vol.
        currency: What every money figure above is in, taken from the
            discount curve rather than assumed.
    """

    delta: float
    gamma: float
    vega: float
    theta: float
    value: float
    rate_bump_bp: float
    vol_bump_bp: float
    currency: str

    def payload_fields(self) -> dict[str, Any]:
        """Every greek with its unit, and the bumps that produced them."""
        return {
            "value": self.value,
            "delta": self.delta,
            "delta_unit": f"{self.currency}_per_bp",
            "gamma": self.gamma,
            "gamma_unit": f"{self.currency}_per_bp_squared",
            "vega": self.vega,
            "vega_unit": f"{self.currency}_per_bp_normal_vol",
            "theta": self.theta,
            "theta_unit": f"{self.currency}_per_day",
            "rate_bump_bp": self.rate_bump_bp,
            "vol_bump_bp": self.vol_bump_bp,
            "method": "bump_and_reprice",
        }


def option_greeks(
    option: Any,
    curve_set: CurveSet,
    volatility: Any,
    *,
    rate_bump_bp: float = BUMP_BP,
    vol_bump_bp: float = 1.0,
    as_of: date | None = None,
    source_evidence: tuple[Evidence, ...] = (),
) -> GreeksResult:
    """Delta, gamma, vega and theta of a swaption or a cap, by bumping and repricing.

    No closed-form greeks. The pricers have them and they would be faster,
    but they would also be a second implementation that can disagree with the
    first, and the two would disagree exactly where it is hardest to notice —
    at a boundary, under a shift, in the degenerate case. Bumping the same
    price function that produces the value costs a handful of evaluations and
    cannot drift from it.

    Vega is always reported per basis point of *normal* volatility. When a
    lognormal quote priced the option, the bump is applied to its at-the-money
    normal equivalent and converted back, so two desks quoting different
    conventions still compare vegas.

    Args:
        option: A :class:`~rates_engine.instruments.swaption.Swaption` or
            :class:`~rates_engine.instruments.capfloor.CapFloor`.
        curve_set: Discount and projection curves.
        volatility: The quote used to price it.
        rate_bump_bp: Curve bump in basis points.
        vol_bump_bp: Volatility bump in basis points of normal volatility.
        as_of: Valuation date. Defaults to the curve's.
        source_evidence: Evidence of the curves and the surface.

    Returns:
        The :class:`GreeksResult`.

    Raises:
        ValueError: Either bump is not positive, or ``option`` is neither a
            swaption nor a cap.
    """
    from rates_engine.instruments.capfloor import CapFloor
    from rates_engine.instruments.swaption import Swaption
    from rates_engine.optionpricing import cap_floor_pv, model_for, swaption_pv
    from rates_engine.volatility.units import Volatility, VolUnits

    if rate_bump_bp <= 0.0 or vol_bump_bp <= 0.0:
        raise ValueError(
            f"bumps must be positive, got rate {rate_bump_bp!r} and vol {vol_bump_bp!r}"
        )
    if isinstance(option, Swaption):
        price = swaption_pv
    elif isinstance(option, CapFloor):
        price = cap_floor_pv  # type: ignore[assignment]
    else:
        raise ValueError(
            f"option greeks are defined for Swaption and CapFloor, got {type(option).__name__}"
        )

    valuation = as_of or curve_set.as_of
    shift = rate_bump_bp * 1e-4

    def at(curves: CurveSet, vol: Any, day: date) -> float:
        return price(option, curves, vol, as_of=day).value

    base = at(curve_set, volatility, valuation)
    up = at(curve_set.shifted(shift), volatility, valuation)
    down = at(curve_set.shifted(-shift), volatility, valuation)
    delta = (down - up) / 2.0 / rate_bump_bp
    gamma = (up + down - 2.0 * base) / (rate_bump_bp * rate_bump_bp)

    # Vega on the normal basis whatever priced it, so the number is comparable.
    if volatility.is_normal:
        bumped_vol = Volatility.unchecked(
            volatility.as_normal_bp() + vol_bump_bp, VolUnits.NORMAL_BP
        )
    else:
        from rates_engine.optionpricing import forward_swap_rate

        forward = (
            forward_swap_rate(option, curve_set)
            if isinstance(option, Swaption)
            else option.caplets[-1].forward_rate(curve_set)
        )
        equivalent = volatility.atm_equivalent_normal(forward).as_normal_bp()
        bumped_vol = Volatility.unchecked(
            (equivalent + vol_bump_bp) * 1e-4 / forward, VolUnits.LOGNORMAL_DECIMAL
        )
    vega = (at(curve_set, bumped_vol, valuation) - base) / vol_bump_bp

    theta = at(curve_set, volatility, valuation + timedelta(days=1)) - base

    return GreeksResult(
        evidence=Evidence(
            produced_by="risk.option_greeks",
            fields={
                "instrument": option.describe(),
                "model": model_for(volatility),
                "volatility": volatility.to_dict(),
                "rate_bump_bp": rate_bump_bp,
                "rate_bump_basis": "zero_curve_parallel",
                "vol_bump_bp": vol_bump_bp,
                "vol_bump_basis": "normal_volatility",
                "theta_basis": "one_calendar_day",
                "difference": "central for delta and gamma, forward for vega and theta",
                "method": "bump_and_reprice",
                "method_note": (
                    "No closed-form greeks: a second implementation can disagree with "
                    "the first exactly where it is hardest to notice."
                ),
                "base_value": base,
            },
            sources=source_evidence,
        ),
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        value=base,
        rate_bump_bp=rate_bump_bp,
        vol_bump_bp=vol_bump_bp,
        currency=curve_set.discount.currency.value,
    )
