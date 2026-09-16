"""The futures-versus-forward convexity adjustment, and where sigma comes from.

A futures contract is margined daily, so a position gains when rates rise at
exactly the moment the gain is reinvested at a higher rate, and loses when
rates fall at the moment the loss is funded more cheaply. That asymmetry makes
the futures rate sit above the forward, and the gap is the convexity
adjustment. It is small at the front — under a basis point inside a year — and
becomes material past two years, which is why a strip used as curve input has
to be adjusted and a single front contract barely cares.

Two models:

``ho_lee``
    ``adjustment = 0.5 * sigma^2 * T1 * T2`` with sigma a normal (absolute)
    volatility. Two parameters fewer than Hull-White and, at the front of the
    curve, indistinguishable from it.
``hull_white``
    Mean reversion kappa and normal volatility sigma. As ``kappa -> 0`` it
    converges to Ho-Lee, which is both a property of the mathematics and a
    test — ``test_convexity.py`` holds the two within 0.01 bp at kappa = 1e-6,
    so a sign error in the Hull-White algebra cannot pass.

**Where sigma is not allowed to come from.** Implied volatility. There are no
options in v1, so there is no implied surface, and inventing one to feed a
curve would put an unobserved parameter under every discount factor. Sigma is
either given explicitly or estimated from realised SOFR, and the evidence says
which.

Calibrating sigma at the money alone overstates the adjustment by 10-25%
(Romero, Bermudez and Turfus). That bites in v2, when an implied surface
exists; it is recorded here so the warning arrives with the code it applies to.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_engine.errors import InsufficientDataError, UnsupportedConventionError
from rates_engine.evidence import DataQuality, Evidence, Provenance
from rates_engine.market.snapshot import MarketSnapshot
from rates_engine.results import EngineResult

__all__ = [
    "ConvexityModel",
    "ConvexityResult",
    "convexity_adjustment",
    "realized_sofr_sigma",
    "MIN_REALIZED_OBSERVATIONS",
    "TRADING_DAYS_PER_YEAR",
]

MIN_REALIZED_OBSERVATIONS = 60
"""Fewest daily changes a realised-volatility estimate is allowed to rest on."""

TRADING_DAYS_PER_YEAR = 252
"""Annualisation factor for a daily realised volatility."""

_KAPPA_FLOOR = 1e-8
"""Below this, the Hull-White expression is evaluated as its Ho-Lee limit.

Not a fudge: the closed form carries ``sigma^2 / (4 kappa)`` against a bracket
that vanishes at the same rate, and below roughly 1e-8 the cancellation eats
more digits than the mean reversion is worth. The limit is exact, and the
evidence records that it was taken.
"""


class ConvexityModel(str):
    """Names of the implemented convexity models, as plain strings with a home."""

    NONE = "none"
    HO_LEE = "ho_lee"
    HULL_WHITE = "hull_white"


_MODELS = (ConvexityModel.NONE, ConvexityModel.HO_LEE, ConvexityModel.HULL_WHITE)


@dataclass(frozen=True)
class ConvexityResult(EngineResult):
    """One contract's convexity adjustment and the assumptions behind it.

    Attributes:
        adjustment_bp: Futures rate minus forward rate, in basis points.
            Always non-negative for non-negative sigma.
        model: Which model produced it.
        sigma: Normal volatility as an absolute annual rate, so 1% is ``0.01``.
        kappa: Mean reversion, ``None`` for Ho-Lee.
        time_to_start: Years to the reference period start.
        time_to_end: Years to the reference period end.
    """

    adjustment_bp: float
    model: str
    sigma: float
    kappa: float | None
    time_to_start: float
    time_to_end: float

    @property
    def adjustment(self) -> float:
        """The adjustment as a decimal rate rather than basis points."""
        return self.adjustment_bp * 1e-4

    def payload_fields(self) -> dict[str, Any]:
        """The adjustment and every parameter that produced it."""
        return {
            "adjustment_bp": self.adjustment_bp,
            "model": self.model,
            "sigma": self.sigma,
            "kappa": self.kappa,
            "time_to_start": self.time_to_start,
            "time_to_end": self.time_to_end,
        }


def _b(kappa: float, span: float) -> float:
    """Hull-White ``B(t, t + span) = (1 - exp(-kappa * span)) / kappa``, stable at small kappa."""
    if kappa == 0.0:
        return span
    return -math.expm1(-kappa * span) / kappa


def convexity_adjustment(
    time_to_start: float,
    time_to_end: float,
    *,
    model: str = ConvexityModel.HO_LEE,
    sigma: float = 0.0,
    kappa: float | None = None,
    sigma_source: str = "explicit",
    sigma_evidence: Evidence | None = None,
) -> ConvexityResult:
    """The futures-minus-forward adjustment for one reference period.

    Args:
        time_to_start: Years from valuation to the period start (``T1``).
        time_to_end: Years from valuation to the period end (``T2``).
        model: ``"none"``, ``"ho_lee"`` or ``"hull_white"``.
        sigma: Normal volatility as an absolute annual rate; 1% is ``0.01``.
        kappa: Mean reversion for Hull-White, in inverse years. Required for
            that model, ignored by the others.
        sigma_source: ``"explicit"`` or ``"realized_sofr"``, recorded in the
            evidence so a reader knows whether the number was observed.
        sigma_evidence: Evidence of how sigma was estimated, chained in.

    Returns:
        The :class:`ConvexityResult`, adjustment in basis points.

    Raises:
        UnsupportedConventionError: ``model`` is not implemented, or
            Hull-White was asked for without a ``kappa``.
        ValueError: Sigma is negative, or the period is not ordered.
    """
    if model not in _MODELS:
        raise UnsupportedConventionError(
            f"convexity model {model!r} is not implemented; supported: {_MODELS}"
        )
    if sigma < 0.0:
        raise ValueError(f"sigma must be non-negative, got {sigma!r}")
    if time_to_end < time_to_start:
        raise ValueError(
            f"period end {time_to_end} precedes start {time_to_start}"
        )

    took_limit = False
    if model == ConvexityModel.NONE:
        adjustment = 0.0
    elif model == ConvexityModel.HO_LEE:
        adjustment = 0.5 * sigma * sigma * time_to_start * time_to_end
    else:
        if kappa is None:
            raise UnsupportedConventionError(
                "hull_white needs a kappa; pass one or use model='ho_lee'"
            )
        if kappa < 0.0:
            raise ValueError(f"kappa must be non-negative, got {kappa!r}")
        if kappa < _KAPPA_FLOOR:
            took_limit = True
            adjustment = 0.5 * sigma * sigma * time_to_start * time_to_end
        else:
            tau = time_to_end - time_to_start
            b_period = _b(kappa, tau)
            b_start = _b(kappa, time_to_start)
            bracket = b_period * (-math.expm1(-2.0 * kappa * time_to_start)) + (
                2.0 * kappa * b_start * b_start
            )
            adjustment = (
                (b_period / tau) * bracket * sigma * sigma / (4.0 * kappa) if tau > 0.0 else 0.0
            )

    evidence = Evidence(
        produced_by="convexity.convexity_adjustment",
        inputs=(
            Provenance(
                source=sigma_source,
                series_id="sigma",
                instrument_kind="normal_volatility",
                data_quality=(
                    DataQuality.OBSERVED
                    if sigma_source == "realized_sofr"
                    else DataQuality.ASSUMED
                ),
                notes=None if sigma_source == "realized_sofr" else "sigma supplied by the caller",
            ),
        ),
        fields={
            "model": model,
            "sigma": sigma,
            "kappa": kappa,
            "sigma_source": sigma_source,
            "time_to_start": time_to_start,
            "time_to_end": time_to_end,
            "hull_white_evaluated_as_ho_lee_limit": took_limit,
            "discounting": "collateral_rate_ois_sofr",
            "discounting_note": (
                "Collateralised flows discount on the OIS-SOFR curve because the "
                "collateral is remunerated at SOFR (Fujii-Shimada-Takahashi; Piterbarg), "
                "not on a separate funding curve."
            ),
            "atm_calibration_caveat": (
                "Calibrating sigma at the money alone overstates the adjustment by "
                "10-25% (Romero-Bermudez-Turfus). Applies from v2, when an implied "
                "surface exists; v1 sigma is explicit or realised."
            ),
        },
        sources=(sigma_evidence,) if sigma_evidence else (),
    )
    return ConvexityResult(
        evidence=evidence,
        adjustment_bp=adjustment * 1e4,
        model=model,
        sigma=sigma,
        kappa=kappa,
        time_to_start=time_to_start,
        time_to_end=time_to_end,
    )


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
