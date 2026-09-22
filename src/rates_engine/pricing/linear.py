"""Present value, par rate and annuity of linear instruments.

Everything here discounts on ``curve_set.discount`` and nothing here decides
what that curve is. Collateralised flows belong on the curve of the rate the
collateral earns -- OIS-SOFR for dollars -- and the evidence of every result
says which, through :mod:`rates_engine.pricing.collateral`, rather than
leaving it to be inferred from the absence of an alternative.

Sensitivities, the parallel DV01 included, are :mod:`rates_engine.risk`'s:
they move the curve and call back into :func:`discounted_value`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rates_engine.conventions.side import Side
from rates_engine.core.errors import CurrencyMismatchError
from rates_engine.core.evidence import Evidence
from rates_engine.core.money import require_same_currency
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.pricing.collateral import (
    collateral_warnings,
    discounting_fields,
    instrument_warnings,
    merge_warnings,
    valuation_warnings,
)
from rates_engine.pricing.projection import Priceable, Swappable, float_leg, project

__all__ = [
    "PriceResult",
    "ParametricComparison",
    "pv",
    "discounted_value",
    "valuation_evidence",
    "par_rate",
    "annuity",
    "price_on_parametric",
]


@dataclass(frozen=True)
class PriceResult(EngineResult):
    """One valuation number, its unit, and what produced it.

    Attributes:
        value: The number.
        measure: ``"pv"``, ``"par_rate"``, ``"annuity"`` or ``"dv01"``.
        unit: The currency for a money amount (``"USD"``, ``"MXN"``),
            ``"<CCY>_per_bp"`` for a sensitivity, or ``"decimal_rate"`` or
            ``"years"``. Carried rather than implied, because a rate without
            a unit and a DV01 quoted per percent are the two classic ways to
            be off by a factor of ten thousand — and a peso amount labelled
            USD is the third, which is why the currency comes from the curve
            that discounted it rather than from a literal.
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

    def __add__(self, other: object) -> PriceResult:
        """Two results of the same measure and unit, added, with both evidence chains.

        A portfolio present value is a sum of leg present values, and before
        this existed the natural way to write one was ``a.value + b.value``,
        which adds pesos to dollars without complaint and throws away both
        evidence chains on the way. Addition is defined here so that the
        obvious spelling is the safe one.

        Refuses a unit mismatch rather than converting: a peso present value
        and a dollar one are not commensurable, and making them so needs a
        rate, a date and a quoting convention that only
        :mod:`rates_engine.pricing.fx_forward` may supply. A measure mismatch — a par rate
        plus an annuity — raises ``TypeError`` instead, because that is a
        mistake in the calling code rather than a problem with the data, and
        :mod:`rates_engine.core.errors` is for the latter.

        The result's evidence names this sum as its producer and carries both
        operands as sources, so ``worst_quality`` degrades to the weaker of
        the two: adding an ``ASSUMED`` peso leg to an ``OBSERVED`` dollar one
        cannot launder the assumption.

        Raises:
            CurrencyMismatchError: The two units differ.
            TypeError: ``other`` is not a :class:`PriceResult`, or the two
                measures differ.
        """
        if not isinstance(other, PriceResult):
            return NotImplemented
        if self.measure != other.measure:
            raise TypeError(
                f"cannot add a {self.measure!r} to a {other.measure!r}: "
                "addition is defined between results of the same measure"
            )
        if self.unit != other.unit:
            raise CurrencyMismatchError(
                f"cannot add {self.unit} to {other.unit}: converting between them "
                "needs a rate, a date and a quoting convention, which is "
                "rates_engine.pricing.fx_forward's job and never an implicit one"
            )
        flows: tuple[Cashflow, ...] | None = None
        if self.cashflows is not None and other.cashflows is not None:
            flows = self.cashflows + other.cashflows
        return PriceResult(
            evidence=Evidence(
                produced_by="pricing.PriceResult.__add__",
                fields={"measure": self.measure, "unit": self.unit, "terms": 2},
                sources=(self.evidence, other.evidence),
            ),
            value=self.value + other.value,
            measure=self.measure,
            unit=self.unit,
            cashflows=flows,
        )


def _pv_of(flows: tuple[Cashflow, ...], curve_set: CurveSet) -> float:
    """Present value, refusing any flow the curve is not denominated to discount.

    The check is here rather than in ``Cashflow`` because this is the one
    place a flow and a curve meet. Without it, pricing a peso swap on the
    dollar curve returns a number and the evidence chain records nothing.
    """
    total = 0.0
    for flow in flows:
        require_same_currency(
            flow.currency,
            curve_set.discount.currency,
            operation=f"discounting a {flow.leg} cashflow paid {flow.payment_date}",
        )
        total += flow.amount * curve_set.discount.df(flow.payment_date)
    return total


def valuation_evidence(
    produced_by: str,
    instrument: Priceable,
    curve_set: CurveSet,
    extra: dict[str, Any],
    sources: tuple[Evidence, ...],
) -> Evidence:
    """The evidence every valuation on a curve set records.

    Public because :mod:`rates_engine.risk` reports DV01 as a valuation
    measure and must describe it exactly as a price is described: the
    instrument, the curve's currency and interpolation, the discounting and
    the curve's own degradations.

    Args:
        produced_by: Stable identifier of the producing call, e.g.
            ``"pricing.pv"``. An identifier, not a module path: it is part of
            the payload and does not move when the code does.
        instrument: What was valued.
        curve_set: The curves it was valued on.
        extra: Measure-specific fields, merged last.
        sources: Evidence of the inputs, chained in.

    Returns:
        The evidence record.
    """
    return Evidence(
        produced_by=produced_by,
        fields={
            "instrument": instrument.describe(),
            "currency": curve_set.currency.value,
            "curve_interpolation": curve_set.discount.interpolation,
            "dual_curve": curve_set.is_dual,
            "projection_curve": "tenor" if curve_set.is_dual else "discount",
            **discounting_fields(curve_set.currency),
            **extra,
        },
        sources=sources,
        # The curve's own degradations, so a price on a curve whose
        # conventions are assumed inherits that without the caller having
        # to remember to pass source_evidence. That forgetting is what made
        # the marking decorative.
        warnings=valuation_warnings(instrument, curve_set),
    )


def discounted_value(instrument: Priceable, curve_set: CurveSet) -> float:
    """Present value as a bare float, for callers that reprice many times.

    Exactly the number :func:`pv` returns, without the cashflow list and the
    evidence record :func:`pv` assembles around it. A risk measure reprices
    two or more times per bucket and discards all of that; this is what it
    calls instead.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        curve_set: Discount and projection curves.

    Returns:
        The present value in the discount curve's currency.

    Raises:
        CurrencyMismatchError: A flow is in another currency than the curve.
    """
    return _pv_of(project(instrument, curve_set), curve_set)


def pv(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    source_evidence: tuple[Evidence, ...] = (),
) -> PriceResult:
    """Present value in USD, signed from the holder's point of view.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves, chained in so that a proxied
            curve stays visibly proxied in the price.

    Returns:
        A :class:`PriceResult` with ``measure="pv"`` and the discount
        curve's currency as its unit.
    """
    flows = project(instrument, curve_set)
    value = _pv_of(flows, curve_set)
    return PriceResult(
        evidence=valuation_evidence("pricing.pv", instrument, curve_set, {"cashflows": len(flows)}, source_evidence),
        value=value,
        measure="pv",
        unit=curve_set.discount.currency.value,
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
    value = _pv_of(unit_swap.fixed_cashflows(), curve_set) / swap.notional
    return PriceResult(
        evidence=valuation_evidence("pricing.annuity", swap, curve_set, {}, source_evidence),
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
    float_pv = _pv_of(float_leg(payer, curve_set), curve_set)
    annuity_value = annuity(swap, curve_set).value
    value = float_pv / (swap.notional * annuity_value)
    return PriceResult(
        evidence=valuation_evidence(
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


@dataclass(frozen=True)
class ParametricComparison(EngineResult):
    """One instrument priced on a fitted curve and on the bootstrapped one.

    PRD-002 AC-4.3. A parametric curve is a smoothing of the market, not the
    market: it does not reprice the calibration instruments exactly, and the
    size of that miss is the whole content of the choice. Returning the
    parametric price alone would hide it, so this returns both and the gap.

    Attributes:
        parametric_pv: PV on the fitted curve, in USD.
        bootstrap_pv: PV on the bootstrapped curve, in USD.
        difference: ``parametric_pv - bootstrap_pv``, in USD. Signed, because
            which way the smoothing pushes the price is information.
        difference_bp_of_notional: The same gap per basis point of notional
            where the instrument declares one, else ``None``. A USD number
            with no scale is not comparable across trades.
        model: The parametric model's name, taken from the fit rather than
            passed in, so the label cannot drift from what produced the curve.
        currency: What both prices are in. The two curve sets are checked to
            agree before either is priced.
    """

    parametric_pv: float
    bootstrap_pv: float
    difference: float
    difference_bp_of_notional: float | None
    model: str
    currency: str

    def payload_fields(self) -> dict[str, Any]:
        """Both prices, the gap, and the curve kind that explains it."""
        return {
            "curve_kind": "parametric",
            "model": self.model,
            "parametric_pv": self.parametric_pv,
            "bootstrap_pv": self.bootstrap_pv,
            "difference": self.difference,
            "difference_bp_of_notional": self.difference_bp_of_notional,
            "unit": self.currency,
        }


def price_on_parametric(
    instrument: Priceable,
    parametric: CurveSet,
    bootstrapped: CurveSet,
    *,
    fit: EngineResult,
) -> ParametricComparison:
    """Price on a fitted curve and report the gap against the bootstrapped one.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        parametric: Curves sampled from the fitted model.
        bootstrapped: The curves the model was fitted to.
        fit: The fit that produced ``parametric``. Its payload must declare
            ``curve_kind="parametric"``; the model name is read from it.

    Returns:
        The :class:`ParametricComparison`.

    Raises:
        ValueError: ``fit`` does not declare itself parametric, so labelling
            the comparison ``curve_kind="parametric"`` would be a claim about
            a curve nothing here has seen.
    """
    # D7: `_pv_of` checks each curve set against its own flows, and nothing
    # checked the two sets against each other — so a parametric USD curve
    # and a bootstrapped MXN one subtracted cleanly into a meaningless
    # number. `CurveSet` makes exactly this check for its own two curves.
    currency = require_same_currency(
        parametric.currency,
        bootstrapped.currency,
        operation="comparing a parametric price with a bootstrapped one",
    )
    fields = fit.payload_fields()
    if fields.get("curve_kind") != "parametric":
        raise ValueError(
            f"{type(fit).__name__} declares curve_kind={fields.get('curve_kind')!r}; "
            "price_on_parametric labels its payload parametric and takes the model "
            "name from the fit, so it will not take a fit that is not one."
        )
    model = str(fields.get("model", type(fit).__name__))

    parametric_pv = _pv_of(project(instrument, parametric), parametric)
    bootstrap_pv = _pv_of(project(instrument, bootstrapped), bootstrapped)
    difference = parametric_pv - bootstrap_pv
    notional = instrument.describe().get("notional")
    per_bp = (
        difference / (abs(float(notional)) * 1e-4)
        if isinstance(notional, (int, float)) and notional
        else None
    )

    evidence = Evidence(
        produced_by="pricing.price_on_parametric",
        fields={
            "instrument": instrument.describe(),
            "curve_kind": "parametric",
            "model": model,
            "parametric_pv": parametric_pv,
            "bootstrap_pv": bootstrap_pv,
            "difference": difference,
            "difference_bp_of_notional": per_bp,
            "currency": currency.value,
            **discounting_fields(currency),
            "note": (
                "A parametric curve smooths the quotes rather than reproducing them, "
                "so this difference is the cost of the smoothing, not an error in "
                "either price."
            ),
        },
        sources=(fit.evidence,),
        warnings=merge_warnings(collateral_warnings(currency), instrument_warnings(instrument)),
    )
    return ParametricComparison(
        evidence=evidence,
        parametric_pv=parametric_pv,
        bootstrap_pv=bootstrap_pv,
        difference=difference,
        difference_bp_of_notional=per_bp,
        model=model,
        currency=currency.value,
    )
