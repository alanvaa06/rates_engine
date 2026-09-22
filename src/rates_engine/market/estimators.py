"""Statistics estimated from observed market data.

Separate from :mod:`rates_engine.models` on purpose: a model is a formula
that takes its parameters as given, and this module is where one of those
parameters -- the normal volatility of SOFR -- is *measured*. Keeping the two
apart is what lets :func:`rates_engine.models.convexity.convexity_adjustment`
run without a market snapshot, and lets the evidence say which of the two a
sigma came from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_engine.core.errors import InsufficientDataError
from rates_engine.core.evidence import Evidence
from rates_engine.core.results import EngineResult
from rates_engine.market.snapshot import MarketSnapshot

__all__ = [
    "SigmaEstimate",
    "realized_sofr_sigma",
    "MIN_REALIZED_OBSERVATIONS",
    "TRADING_DAYS_PER_YEAR",
]

MIN_REALIZED_OBSERVATIONS = 60
"""Fewest daily changes a realised-volatility estimate is allowed to rest on."""

TRADING_DAYS_PER_YEAR = 252
"""Annualisation factor for a daily realised volatility."""


@dataclass(frozen=True)
class SigmaEstimate(EngineResult):
    """A realised-volatility estimate of sigma and the window it came from.

    Attributes:
        sigma: Annualised normal volatility as an absolute rate.
        window: Number of daily changes requested.
        observations: Number actually used.
        start: First fixing date in the window.
        end: Last fixing date in the window.
    """

    sigma: float
    window: int
    observations: int
    start: date
    end: date

    def payload_fields(self) -> dict[str, Any]:
        """Sigma and the window behind it."""
        return {
            "sigma": self.sigma,
            "window": self.window,
            "observations": self.observations,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


def realized_sofr_sigma(
    snapshot: MarketSnapshot,
    *,
    window: int = 252,
    series_id: str = "SOFR",
    as_of: date | None = None,
) -> SigmaEstimate:
    """Estimate the normal volatility of SOFR from its own recent history.

    Daily changes in the overnight rate, annualised by the square root of 252.
    Normal rather than lognormal because the convexity formulas above are
    written in absolute rate terms, and because a lognormal volatility is
    undefined at a zero rate that SOFR has been near before.

    Args:
        snapshot: Snapshot holding the fixings.
        window: Number of daily changes to use.
        series_id: Overnight series to estimate from.
        as_of: Last date considered. Defaults to the snapshot's own.

    Returns:
        The :class:`SigmaEstimate`.

    Raises:
        InsufficientDataError: Fewer than
            :data:`MIN_REALIZED_OBSERVATIONS` changes are available. A
            volatility from thirty observations is a number, not an estimate.
        MissingFixingError: The snapshot has no such series.
    """
    series = snapshot.require(series_id)
    cutoff = as_of or snapshot.as_of
    usable = [(d, v) for d, v in zip(series.dates, series.values, strict=True) if d <= cutoff]
    tail = usable[-(window + 1) :]
    changes = [b[1] - a[1] for a, b in zip(tail, tail[1:], strict=False)]
    if len(changes) < MIN_REALIZED_OBSERVATIONS:
        raise InsufficientDataError(
            f"realised sigma needs at least {MIN_REALIZED_OBSERVATIONS} daily changes of "
            f"{series_id!r} on or before {cutoff}; the snapshot supports {len(changes)}"
        )
    mean = sum(changes) / len(changes)
    variance = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
    sigma = math.sqrt(variance * TRADING_DAYS_PER_YEAR)
    evidence = Evidence(
        produced_by="convexity.realized_sofr_sigma",
        inputs=(series.provenance,),
        fields={
            "series_id": series_id,
            "window": window,
            "observations": len(changes),
            "start": tail[0][0].isoformat(),
            "end": tail[-1][0].isoformat(),
            "annualisation": TRADING_DAYS_PER_YEAR,
            "sigma": sigma,
            "basis": "normal_absolute_rate",
        },
    )
    return SigmaEstimate(
        evidence=evidence,
        sigma=sigma,
        window=window,
        observations=len(changes),
        start=tail[0][0],
        end=tail[-1][0],
    )
