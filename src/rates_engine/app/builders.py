"""Turning configuration blocks into domain objects.

Every interface needs the same translation -- a ``curve`` block into
calibration nodes, a ``swap`` block into an :class:`OISSwap`, an ``fx`` block
into two curves -- so it is done once, here, rather than in whichever
adapter happened to need it first.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from rates_engine.app.config import as_date
from rates_engine.conventions.daycount import CURVE_TIME_BASIS, year_fraction
from rates_engine.conventions.indices import SOFR, index_named
from rates_engine.conventions.side import Side
from rates_engine.core.errors import ConfigurationError
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.core.money import Currency
from rates_engine.curves.bootstrap import FuturesNode, RealizedStubNode
from rates_engine.curves.discount import DiscountCurve
from rates_engine.instruments.swaps import OISSwap
from rates_engine.models.convexity import ConvexityModel, convexity_adjustment
from rates_engine.pricing.calibration import SwapQuoteNode

__all__ = ["calibration_nodes", "ois_swap", "flat_curve", "fx_inputs"]


def calibration_nodes(config: dict[str, Any], as_of: date) -> tuple[Any, ...]:
    """Build calibration instruments from the ``curve`` block of a config.

    Futures prices are convexity-adjusted here, with the model and sigma the
    block names, so the node carries the adjusted forward and the record of
    how it was adjusted.

    Args:
        config: The whole configuration mapping.
        as_of: Valuation date, from which contract times are measured in
            years on the curve's time basis.

    Returns:
        The stub, futures and par swap nodes, in the order given. Each
        ``par_swaps`` entry becomes a
        :class:`~rates_engine.pricing.calibration.SwapQuoteNode` on the SOFR
        OIS it describes.

    Raises:
        ConfigurationError: A ``par_swaps`` entry uses the v0.3 shape or is
            missing a required key.
    """
    curve = config.get("curve") or {}
    instruments: list[Any] = []

    stub = curve.get("stub")
    if stub:
        instruments.append(
            RealizedStubNode(end=as_date(stub["end"]), accrual_factor=float(stub["accrual_factor"]))
        )

    convexity = curve.get("convexity") or {}
    model = convexity.get("model", ConvexityModel.NONE)
    sigma = float(convexity.get("sigma", 0.0))
    kappa = convexity.get("kappa")

    for entry in curve.get("futures", ()):
        start, end = as_date(entry["start"]), as_date(entry["end"])
        adjustment = convexity_adjustment(
            year_fraction(as_of, start, CURVE_TIME_BASIS),
            year_fraction(as_of, end, CURVE_TIME_BASIS),
            model=model,
            sigma=sigma,
            kappa=float(kappa) if kappa is not None else None,
        )
        instruments.append(
            FuturesNode(
                start=start,
                end=end,
                forward_rate=(100.0 - float(entry["price"])) / 100.0 - adjustment.adjustment,
                label=str(entry.get("label", f"future:{entry['start']}")),
                convexity={
                    "model": model,
                    "sigma": sigma,
                    "kappa": kappa,
                    "adjustment_bp": adjustment.adjustment_bp,
                },
            )
        )

    for number, entry in enumerate(curve.get("par_swaps", ())):
        instruments.append(_par_swap_quote(entry, number))
    return tuple(instruments)


_LEGACY_PAR_SWAP_KEYS = ("start", "payment_dates", "year_fractions")
_PAR_SWAP_REQUIRED = ("effective", "maturity", "rate")


def _par_swap_quote(entry: dict[str, Any], number: int) -> SwapQuoteNode:
    """One ``par_swaps`` entry as a quote on a real SOFR OIS.

    The entry states the swap's terms and the node reprices that swap with
    the pricer itself, payment lag included. The v0.3 shape -- a start, a
    list of payment dates and their year fractions -- built a node that
    assumed each period is paid the day it ends, which misprices a SOFR OIS
    (paid two business days later) by several basis points at the front; it
    is refused with the migration spelled out rather than accepted.
    """
    legacy = [key for key in _LEGACY_PAR_SWAP_KEYS if key in entry]
    if legacy:
        raise ConfigurationError(
            f"curve.par_swaps[{number}] uses the v0.3 keys {legacy}. A par quote is now "
            "given as the swap it quotes: effective, maturity and rate, with optional "
            "frequency_months (12), payment_lag_days (2) and label. The dates and year "
            "fractions are generated from those terms, payment lag included; the old "
            "shape ignored the lag and mispriced a SOFR OIS by several basis points."
        )
    missing = [key for key in _PAR_SWAP_REQUIRED if key not in entry]
    if missing:
        raise ConfigurationError(f"curve.par_swaps[{number}] is missing {missing}")
    rate = float(entry["rate"])
    swap = OISSwap(
        effective=as_date(entry["effective"]),
        maturity=as_date(entry["maturity"]),
        fixed_rate=rate,
        frequency_months=int(entry.get("frequency_months", 12)),
        payment_lag_days=int(entry.get("payment_lag_days", 2)),
    )
    label = str(entry.get("label", ""))
    return SwapQuoteNode(
        swap,
        rate,
        label=label,
        provenance=Provenance(
            source=str(entry.get("source", "file")),
            series_id=label or None,
            instrument_kind=str(entry.get("instrument_kind", "ois_par")),
            data_quality=DataQuality(entry.get("data_quality", "observed")),
        ),
    )


def ois_swap(config: dict[str, Any]) -> OISSwap:
    """Build the OIS swap described by the ``swap`` block of a config.

    Args:
        config: The whole configuration mapping.

    Returns:
        The swap, with notional in currency units and rates as decimals. An
        optional ``index`` names the overnight rate it floats on (``"SOFR"``
        by default, or ``"TIIE_FONDEO"``); the index fixes its currency and
        calendar.

    Raises:
        UnsupportedConventionError: ``index`` names no defined rate, or a term
            rate.
    """
    spec = config["swap"]
    return OISSwap(
        effective=as_date(spec["effective"]),
        maturity=as_date(spec["maturity"]),
        fixed_rate=float(spec["fixed_rate"]),
        notional=float(spec.get("notional", 1_000_000.0)),
        side=str(spec.get("side", Side.PAYER)),
        frequency_months=int(spec.get("frequency_months", 12)),
        payment_lag_days=int(spec.get("payment_lag_days", 2)),
        index=index_named(str(spec.get("index", SOFR.name))),
    )


def flat_curve(as_of: date, rate: float, currency: Currency, years: float) -> DiscountCurve:
    """A single-node continuous curve, for the FX commands.

    The FX commands take two rates rather than two bootstrapped curves,
    because a treasurer comparing hedge structures has a deposit rate to
    hand and not a calibration set. The curve is built here so the forward
    goes through the same discounting code every other price does, rather
    than through a second exponential written out in the command.

    Args:
        as_of: Valuation date.
        rate: Continuously compounded rate as a decimal a year.
        currency: What the curve discounts.
        years: Horizon in years; the single node sits at the nearest day.

    Returns:
        The curve.
    """
    node = as_of + timedelta(days=max(1, round(365.0 * years)))
    span = year_fraction(as_of, node, CURVE_TIME_BASIS)
    return DiscountCurve(as_of, (node,), (math.exp(-rate * span),), currency=currency)


def fx_inputs(config: dict[str, Any]) -> tuple[date, dict[str, Any], float]:
    """The valuation date, the ``fx`` block and the years to delivery.

    Args:
        config: The whole configuration mapping.

    Returns:
        ``(as_of, fx_block, years_to_delivery)``, years on the curve's time
        basis.

    Raises:
        ConfigurationError: The ``fx`` block is missing or empty.
    """
    block = config.get("fx") or {}
    if not block:
        raise ConfigurationError(
            "this command needs an `fx` block with spot, delivery, r_domestic and "
            "r_foreign. See `rateng describe --json` for the shape."
        )
    as_of = as_date(config["as_of"])
    delivery = as_date(block["delivery"])
    return as_of, block, year_fraction(as_of, delivery, CURVE_TIME_BASIS)
