"""Present value, par rate and annuity of linear instruments.

Everything here discounts on ``curve_set.discount`` and nothing here decides
what that curve is. Collateralised flows belong on the curve of the rate the
collateral earns -- OIS-SOFR for dollars -- and the evidence of every result
says which, through :mod:`rates_engine.pricing.collateral`, rather than
leaving it to be inferred from the absence of an alternative.

Sensitivities, the parallel DV01 included, are :mod:`rates_engine.risk`'s:
they move the curve and call back into :func:`discounted_value`.

**A flow paid on or before the valuation date is worth nothing.** It has
left the trade. Until v0.4 every flow was discounted, and
``DiscountCurve.df`` is one on or before ``as_of``, so a swap struck two
years ago carried its paid coupons at face value: on a three-year 4% payer
over USD 100m valued eighteen months in, the fixed coupon paid six months
earlier came back as -4.06m USD. Which flows are paid is
:func:`~rates_engine.pricing.projection.is_paid`'s to say; the built-in
projections ask it per period, and :func:`_live` asks it again of whatever
a caller's own projection returns. The instrument's currency is checked
before anything is projected, and every flow's before that cut, so a trade
in the wrong currency is refused as that -- matured, or seasoned and
missing the fixings the projection would ask for first.

**A floating coupon already decided needs its fixings.** Every valuation
here takes ``fixings``, a :class:`~rates_engine.market.snapshot.MarketSnapshot`
of published rates, and hands it to the projection, which compounds or
reads that history -- an overnight period that has begun, a term period
whose fixing date has passed -- and refuses with
:class:`~rates_engine.core.errors.MissingFixingError` when it is absent
(:mod:`rates_engine.pricing.projection` says exactly what it reads). A
trade with no such coupon needs none and prices to the bit as it did
before; handed fixings anyway, it ignores them. The fixings a price
did use are in its evidence, as ``realised_fixings`` and as the series'
provenance among the inputs, so their data quality reaches
``worst_quality``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_engine.conventions.side import Side
from rates_engine.core.errors import CurrencyMismatchError
from rates_engine.core.evidence import Evidence, Provenance
from rates_engine.core.money import require_same_currency
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.market.snapshot import MarketSnapshot
from rates_engine.pricing.collateral import (
    collateral_warnings,
    discounting_fields,
    instrument_warnings,
    merge_warnings,
    valuation_warnings,
)
from rates_engine.pricing.projection import (
    Priceable,
    Swappable,
    float_leg,
    is_paid,
    project,
)

__all__ = [
    "PriceResult",
    "ParametricComparison",
    "pv",
    "discounted_value",
    "annuity_value",
    "par_rate_value",
    "valuation_evidence",
    "discounted_value_with_history",
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


def _require_currency(instrument: Priceable, curve_set: CurveSet) -> None:
    """Refuse an instrument paid in another currency than the curve, whatever is left of it.

    Called before the instrument is projected. The projection asks for the
    fixings a seasoned trade needs before it produces a flow, so a check on
    the flows alone answered a peso OIS from 2025-07-15 on the dollar curve
    with ``MissingFixingError`` (exit 1, "pass the published fixings"), and
    the caller who did so reached ``CurrencyMismatchError`` (exit 2) only on
    the second try. And the built-in projections never produce a paid flow,
    so a flow check never sees a matured trade at all: a peso swap that
    finished last year came back as 0.0. :func:`_live` still checks every
    flow, which catches a caller's own product that declares one currency
    and pays another.
    """
    require_same_currency(
        instrument.currency,
        curve_set.discount.currency,
        operation=f"valuing a {type(instrument).__name__}",
    )


def _live(flows: tuple[Cashflow, ...], curve_set: CurveSet) -> tuple[Cashflow, ...]:
    """Every flow checked against the curve's currency, then the ones still to be paid.

    The check is here rather than in ``Cashflow`` because this is where a
    flow meets a curve, and it runs on paid flows too: a caller's own
    projection can return a whole history. Which flows are paid is
    :func:`~rates_engine.pricing.projection.is_paid`'s to say.
    """
    as_of = curve_set.as_of
    for flow in flows:
        require_same_currency(
            flow.currency,
            curve_set.discount.currency,
            operation=f"discounting a {flow.leg} cashflow paid {flow.payment_date}",
        )
    return tuple(flow for flow in flows if not is_paid(flow.payment_date, as_of))


def _pv_of(flows: tuple[Cashflow, ...], curve_set: CurveSet) -> float:
    """Present value of flows :func:`_live` has already checked and cut."""
    total = 0.0
    for flow in flows:
        total += flow.amount * curve_set.discount.df(flow.payment_date)
    return total


def _priced_flows(
    instrument: Priceable, curve_set: CurveSet, fixings: MarketSnapshot | None
) -> tuple[Cashflow, ...]:
    """The flows a value counts: checked for currency, projected and cut at ``as_of``."""
    _require_currency(instrument, curve_set)
    return _live(project(instrument, curve_set, **_passing(fixings)), curve_set)


def _passing(fixings: MarketSnapshot | None) -> dict[str, MarketSnapshot]:
    """``fixings`` as a keyword for ``project`` and ``float_leg``, or nothing.

    A projection registered for a product of the caller's own may predate
    the keyword. Passing it only when there is something to pass keeps such
    a product pricing exactly as before; handing it fixings it cannot take
    fails loudly, in Python's own words, rather than dropping them.
    """
    return {} if fixings is None else {"fixings": fixings}


def _period(flow: Cashflow) -> dict[str, Any]:
    return {
        "leg": flow.leg,
        "accrual_start": flow.accrual_start.isoformat(),
        "accrual_end": flow.accrual_end.isoformat(),
        "payment_date": flow.payment_date.isoformat(),
    }


def _history_used(
    flows: Iterable[Cashflow], instrument: object, as_of: date
) -> tuple[dict[str, Any], tuple[Provenance, ...]]:
    """What the flows' floating amounts rest on, for the evidence, and the fixings' provenance.

    ``realised_fixings``: one record per flow paid partly or wholly on
    published fixings -- the period, then what was read for it -- with one
    provenance per series among the inputs, in the order the flows first
    used it.

    ``fixings_dated_as_of``: one entry per term period whose fixing falls on
    the valuation date itself, saying whether that fixing came from the
    snapshot (``"source": "fixings"``) or, not out yet, the rate was
    forecast off the curve (``"source": "curve"``), as QuantLib does. The
    two give the same trade different numbers, so the price says which.

    Each field is left out when it would be empty, so the evidence of a
    trade with no history is exactly what it always was.

    The forecast is recorded and not degraded. With T+2 spot the first
    period of every Term SOFR swap struck today fixes on ``as_of``, so a
    degradation turned the ordinary morning quote of a vanilla swap, and
    everything built on it, ``proxy`` until the print was out; and a risk
    measure, which keeps no flows, reported the same trade ``observed``.
    """
    realised: list[dict[str, Any]] = []
    dated_today: list[dict[str, Any]] = []
    inputs: dict[Provenance, None] = {}
    index = getattr(instrument, "rate_index", None)
    for flow in flows:
        if flow.fixing is not None:
            realised.append({**_period(flow), **flow.fixing.to_dict()})
            inputs.setdefault(flow.fixing.provenance)
            if flow.fixing.method == "term" and flow.fixing.observed_from == as_of:
                dated_today.append(
                    {
                        **_period(flow),
                        "series_id": flow.fixing.series_id,
                        "fixing_date": as_of.isoformat(),
                        "source": "fixings",
                    }
                )
        elif flow.projected_fixing_date is not None:
            dated_today.append(
                {
                    **_period(flow),
                    "series_id": index.name if index is not None else "unknown",
                    "fixing_date": flow.projected_fixing_date.isoformat(),
                    "source": "curve",
                }
            )
    fields: dict[str, Any] = {}
    if realised:
        fields["realised_fixings"] = realised
    if dated_today:
        fields["fixings_dated_as_of"] = dated_today
    return fields, tuple(inputs)


def _history_record(
    instrument: Priceable, flows: Iterable[Cashflow], as_of: date
) -> Evidence | None:
    """The part of a price's evidence that says what its floating coupons rest on, or ``None``."""
    history, inputs = _history_used(flows, instrument, as_of)
    if not history:
        return None
    return Evidence(
        produced_by="pricing.history",
        inputs=inputs,
        fields={"instrument": instrument.describe(), **history},
    )


def valuation_evidence(
    produced_by: str,
    instrument: Priceable,
    curve_set: CurveSet,
    extra: dict[str, Any],
    sources: tuple[Evidence, ...],
    *,
    realised: Iterable[Cashflow] = (),
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
        realised: The flows behind the result. Each one whose amount rests on
            published fixings adds a ``realised_fixings`` entry to the fields
            and its series' provenance to the inputs; each term period fixed
            on ``as_of`` itself adds a ``fixings_dated_as_of`` entry saying
            whether that fixing was read or forecast. A result with none adds
            nothing, so its payload is what it always was.

    Returns:
        The evidence record.
    """
    history, inputs = _history_used(realised, instrument, curve_set.as_of)
    return Evidence(
        produced_by=produced_by,
        inputs=inputs,
        fields={
            "instrument": instrument.describe(),
            "currency": curve_set.currency.value,
            "curve_interpolation": curve_set.discount.interpolation,
            "dual_curve": curve_set.is_dual,
            "projection_curve": "tenor" if curve_set.is_dual else "discount",
            **discounting_fields(curve_set.currency),
            **extra,
            **history,
        },
        sources=sources,
        # The curve's own degradations, so a price on a curve whose
        # conventions are assumed inherits that without the caller having
        # to remember to pass source_evidence. That forgetting is what made
        # the marking decorative.
        warnings=valuation_warnings(instrument, curve_set),
    )


def discounted_value(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    fixings: MarketSnapshot | None = None,
) -> float:
    """Present value as a bare float, for callers that reprice many times.

    Exactly the number :func:`pv` returns, without the cashflow list and the
    evidence record :func:`pv` assembles around it. A risk measure reprices
    two or more times per bucket and discards all of that; this is what it
    calls instead.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        curve_set: Discount and projection curves.
        fixings: Published fixings for a floating coupon already decided on
            ``curve_set.as_of``; see :func:`pv`.

    Returns:
        The present value in the discount curve's currency.

    Raises:
        CurrencyMismatchError: The instrument, or a flow, is in another
            currency than the curve. Checked before any fixing is asked for.
        MissingFixingError: A floating coupon is already decided and ``fixings``
            does not cover its history.
    """
    return _pv_of(_priced_flows(instrument, curve_set, fixings), curve_set)


def discounted_value_with_history(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    fixings: MarketSnapshot | None = None,
) -> tuple[float, Evidence | None]:
    """:func:`discounted_value`, and the record of the history it read, from one projection.

    A risk measure reprices on moved curves and keeps none of the flows, so
    the fixings behind them, and any forecast standing in for a fixing dated
    ``as_of``, would be missing from its evidence. This is that part of a
    price's evidence on its own, for the measure to chain in as a source:
    ``realised_fixings`` and ``fixings_dated_as_of`` in the fields, and the
    fixings' provenance among the inputs, so ``worst_quality`` sees their
    data quality. The forecast is recorded, not degraded, as in a price.

    A history does not move with the curve: the fixings read, and which
    period fixes on ``as_of``, are the same on every curve a measure moves
    to. So the record of any one reprice is the record of them all, and a
    measure takes it from a reprice it makes anyway. Recording it from a
    projection of its own, as the first cut of this did, cost a spot
    ten-year OIS a third more per DV01.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        curve_set: Discount and projection curves, moved or not.
        fixings: Published fixings, as for :func:`pv`.

    Returns:
        The present value in the discount curve's currency, exactly
        :func:`discounted_value`'s, and the evidence record, or ``None``
        when no floating coupon rests on a fixing or on a forecast of one
        dated ``as_of``.

    Raises:
        CurrencyMismatchError: As :func:`discounted_value`.
        MissingFixingError: A floating coupon is already decided and ``fixings``
            does not cover its history.
    """
    flows = _priced_flows(instrument, curve_set, fixings)
    return _pv_of(flows, curve_set), _history_record(instrument, flows, curve_set.as_of)


def annuity_value(swap: Swappable, curve_set: CurveSet) -> float:
    """Fixed-leg annuity per unit notional as a bare float; see :func:`annuity`.

    Args:
        swap: A two-legged instrument.
        curve_set: Discount and projection curves.

    Returns:
        The annuity in years of discounted accrual.
    """
    _require_currency(swap, curve_set)
    unit_swap = swap.with_terms(fixed_rate=1.0, side=Side.RECEIVER)
    return _pv_of(_live(unit_swap.fixed_cashflows(), curve_set), curve_set) / swap.notional


def _float_leg(
    swap: Swappable, curve_set: CurveSet, fixings: MarketSnapshot | None
) -> tuple[Cashflow, ...]:
    """The payer's floating flows still to be paid, which a par rate prices."""
    _require_currency(swap, curve_set)
    payer = swap.with_terms(side=Side.PAYER)
    return _live(float_leg(payer, curve_set, **_passing(fixings)), curve_set)


def par_rate_value(
    swap: Swappable,
    curve_set: CurveSet,
    *,
    fixings: MarketSnapshot | None = None,
) -> float:
    """The par rate as a bare float: exactly :func:`par_rate`'s value.

    For callers that evaluate it many times -- a root solver calibrating a
    curve node to the swap, above all -- and would otherwise build and
    discard an evidence record per trial.

    Args:
        swap: A two-legged instrument.
        curve_set: Discount and projection curves.
        fixings: Published fixings for a floating coupon already decided; see
            :func:`par_rate`.

    Returns:
        The par rate as a decimal.

    Raises:
        MissingFixingError: A floating coupon is already decided and ``fixings``
            does not cover its history.
    """
    float_pv = _pv_of(_float_leg(swap, curve_set, fixings), curve_set)
    return float_pv / (swap.notional * annuity_value(swap, curve_set))


def pv(
    instrument: Priceable,
    curve_set: CurveSet,
    *,
    source_evidence: tuple[Evidence, ...] = (),
    fixings: MarketSnapshot | None = None,
) -> PriceResult:
    """Present value in USD, signed from the holder's point of view.

    A trade that started before ``curve_set.as_of`` is valued on what is
    left of it: paid flows count for nothing, the fixed coupon accruing
    today counts in full, and a floating coupon already decided is its
    published history -- compounded for an overnight index over the days
    before ``as_of``, the fixing itself for a term index that has fixed --
    and the curve's forecast for the rest.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves, chained in so that a proxied
            curve stays visibly proxied in the price.
        fixings: Published fixings, needed only when a floating coupon is
            already decided on ``curve_set.as_of``: an overnight period that
            has begun, or a term period whose fixing date -- two business days
            before the start for Term SOFR, the start for TIIE 28 -- is before
            ``as_of``. A term fixing dated ``as_of`` is read if present and
            forecast off the curve if not, as QuantLib does, so a swap struck
            at spot today prices without one; the evidence's
            ``fixings_dated_as_of`` says which, and the forecast is not a
            degradation. A snapshot passed without the index's series is
            refused even for that fixing. A series named after the index
            (``"SOFR"``, ``"TERM_SOFR_3M"``, ``"TIIE_FONDEO"``, ``"TIIE_28"``)
            holding one decimal rate per business day. Overnight fixings dated
            on or after ``as_of`` are never read, nor term fixings after it.

    Returns:
        A :class:`PriceResult` with ``measure="pv"`` and the discount
        curve's currency as its unit. Its ``cashflows`` are the flows the
        value counts: none paid on or before the valuation date.

    Raises:
        CurrencyMismatchError: The instrument, or a flow, is in another
            currency than the curve. Checked before any fixing is asked for.
        MissingFixingError: A floating coupon is already decided and ``fixings``
            is absent, lacks the index's series, or lacks a business day of
            its history; or a term coupon fixes on ``as_of`` and ``fixings``
            lacks the index's series. The message names the period and the
            series.
    """
    flows = _priced_flows(instrument, curve_set, fixings)
    value = _pv_of(flows, curve_set)
    return PriceResult(
        evidence=valuation_evidence(
            "pricing.pv",
            instrument,
            curve_set,
            {"cashflows": len(flows)},
            source_evidence,
            realised=flows,
        ),
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

    Counts the coupons still to be paid, the one accruing today in full:
    the same set QuantLib's fixed-leg BPS counts. No fixing is needed, since
    the fixed leg has none.

    Args:
        swap: A two-legged instrument.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves.

    Returns:
        A :class:`PriceResult` with ``measure="annuity"`` and ``unit="years"``.
    """
    value = annuity_value(swap, curve_set)
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
    fixings: MarketSnapshot | None = None,
) -> PriceResult:
    """The fixed rate that prices the swap at zero, as a decimal.

    Computed as the floating leg's present value over the annuity, both taken
    on the same curve set, so the two-curve case falls out without a branch:
    the floating leg finds its forwards on the projection curve and both legs
    discount on the OIS curve.

    For a swap that has started it is the rate that zeroes what is left --
    QuantLib's ``fairRate`` -- with the floating coupon in progress, history
    and forecast, in the numerator and the fixed coupon in progress, in full,
    in the annuity.

    Args:
        swap: A two-legged instrument.
        curve_set: Discount and projection curves.
        source_evidence: Evidence of the curves.
        fixings: Published fixings for a floating coupon already decided; see
            :func:`pv`.

    Returns:
        A :class:`PriceResult` with ``measure="par_rate"`` and
        ``unit="decimal_rate"``.

    Raises:
        ZeroDivisionError: The annuity is zero, which means the fixed leg has
            no periods left to discount.
        MissingFixingError: A floating coupon is already decided and ``fixings``
            does not cover its history.
    """
    floating = _float_leg(swap, curve_set, fixings)
    float_pv = _pv_of(floating, curve_set)
    years = annuity_value(swap, curve_set)
    value = float_pv / (swap.notional * years)
    return PriceResult(
        evidence=valuation_evidence(
            "pricing.par_rate",
            swap,
            curve_set,
            {"annuity_years": years, "float_leg_pv": float_pv},
            source_evidence,
            realised=floating,
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
    fixings: MarketSnapshot | None = None,
) -> ParametricComparison:
    """Price on a fitted curve and report the gap against the bootstrapped one.

    Args:
        instrument: Anything :func:`~rates_engine.pricing.projection.project` handles.
        parametric: Curves sampled from the fitted model.
        bootstrapped: The curves the model was fitted to.
        fit: The fit that produced ``parametric``. Its payload must declare
            ``curve_kind="parametric"``; the model name is read from it.
        fixings: Published fixings for a floating coupon already decided; see
            :func:`pv`. The same history enters both prices.

    Returns:
        The :class:`ParametricComparison`.

    Raises:
        ValueError: ``fit`` does not declare itself parametric, so labelling
            the comparison ``curve_kind="parametric"`` would be a claim about
            a curve nothing here has seen.
        MissingFixingError: A floating coupon is already decided and ``fixings``
            does not cover its history.
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

    parametric_flows = _priced_flows(instrument, parametric, fixings)
    parametric_pv = _pv_of(parametric_flows, parametric)
    bootstrap_pv = _pv_of(_priced_flows(instrument, bootstrapped, fixings), bootstrapped)
    # The history is the same on both curves; record it once.
    history, inputs = _history_used(parametric_flows, instrument, parametric.as_of)
    difference = parametric_pv - bootstrap_pv
    notional = instrument.describe().get("notional")
    per_bp = (
        difference / (abs(float(notional)) * 1e-4)
        if isinstance(notional, (int, float)) and notional
        else None
    )

    evidence = Evidence(
        produced_by="pricing.price_on_parametric",
        inputs=inputs,
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
            **history,
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
