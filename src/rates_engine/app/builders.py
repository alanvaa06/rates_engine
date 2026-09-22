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
from rates_engine.conventions.daycount import year_fraction
from rates_engine.conventions.side import Side
from rates_engine.core.errors import ConfigurationError
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.core.money import Currency
from rates_engine.curves.bootstrap import FuturesNode, ParSwapNode, RealizedStubNode
from rates_engine.curves.discount import CURVE_TIME_BASIS, DiscountCurve
from rates_engine.instruments.swaps import OISSwap
from rates_engine.models.convexity import ConvexityModel, convexity_adjustment

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
        The stub, futures and par swap nodes, in the order given.
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

    for entry in curve.get("par_swaps", ()):
        payments = tuple(as_date(d) for d in entry["payment_dates"])
        instruments.append(
            ParSwapNode(
                start=as_date(entry["start"]),
                payment_dates=payments,
                year_fractions=tuple(float(x) for x in entry["year_fractions"]),
                quoted_rate=float(entry["rate"]),
                label=str(entry.get("label", f"par:{entry['payment_dates'][-1]}")),
                provenance=Provenance(
                    source=str(entry.get("source", "file")),
                    series_id=str(entry.get("label", "par_swap")),
                    instrument_kind=str(entry.get("instrument_kind", "ois_par")),
                    data_quality=DataQuality(entry.get("data_quality", "observed")),
                ),
            )
        )
    return tuple(instruments)


def ois_swap(config: dict[str, Any]) -> OISSwap:
    """Build the OIS swap described by the ``swap`` block of a config.

    Args:
        config: The whole configuration mapping.

    Returns:
        The swap, with notional in currency units and rates as decimals.
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
