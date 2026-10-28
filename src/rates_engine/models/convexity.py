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
from enum import StrEnum
from typing import Any

from rates_engine.conventions.indices import SOFR
from rates_engine.core.errors import ImplausibleInputError, UnsupportedConventionError
from rates_engine.core.evidence import DataQuality, Evidence, Provenance
from rates_engine.core.results import EngineResult

__all__ = [
    "ConvexityModel",
    "ConvexityResult",
    "convexity_adjustment",
    "MAX_PLAUSIBLE_SIGMA",
]

MAX_PLAUSIBLE_SIGMA = 0.10
"""Largest normal volatility accepted, as a decimal annual rate: 1000 bp a year.

The same ceiling :class:`~rates_engine.volatility.units.Volatility` puts on a
normal volatility. SOFR's realised normal vol has run at tens of basis points
a year, and a few hundred in a stressed window, so this sits well beyond any
market. What it catches is units: a sigma typed in percent (``1.0`` for 1%)
gave 22,500 bp of adjustment at two years, and one typed in basis points
(``100``) gave 225,000,000 bp, both without a word. It is a plausibility band,
not a measurement; widen it deliberately if you mean it.
"""

_KAPPA_FLOOR = 1e-8
"""Below this, the Hull-White expression is evaluated as its Ho-Lee limit.

Not a fudge: the closed form carries ``sigma^2 / (4 kappa)`` against a bracket
that vanishes at the same rate, and below roughly 1e-8 the cancellation eats
more digits than the mean reversion is worth. The limit is exact, and the
evidence records that it was taken.
"""


class ConvexityModel(StrEnum):
    """The implemented convexity models.

    ``NONE``
        No adjustment. Honest when sigma is unknown and the strip is short.
    ``HO_LEE``
        ``0.5 * sigma^2 * T1 * T2``, one parameter.
    ``HULL_WHITE``
        Mean reversion and volatility, converging to Ho-Lee as kappa vanishes.

    A :class:`~enum.StrEnum`, so a plain ``"ho_lee"`` from a config file
    compares equal to the member and no caller has to import the enum to pass
    a model by name.
    """

    NONE = "none"
    HO_LEE = "ho_lee"
    HULL_WHITE = "hull_white"


_MODELS = tuple(ConvexityModel)


@dataclass(frozen=True)
class ConvexityResult(EngineResult):
    """One contract's convexity adjustment and the assumptions behind it.

    Attributes:
        adjustment_bp: Futures rate minus forward rate, in basis points.
            Non-negative: sigma is refused below zero, and a period that has
            already started (``time_to_start < 0``) is given zero, the value
            both models take at ``time_to_start = 0``.
        model: Which model produced it.
        sigma: Normal volatility as an absolute annual rate, so 1% (100 bp a
            year) is ``0.01``.
        kappa: Mean reversion, ``None`` for Ho-Lee.
        time_to_start: Years to the reference period start, as given; negative
            for a period already under way.
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


def _require_kappa(kappa: float | None) -> float:
    """Hull-White's mean reversion, refused when missing or negative."""
    if kappa is None:
        raise UnsupportedConventionError(
            "hull_white needs a kappa; pass one or use model='ho_lee'"
        )
    if kappa < 0.0:
        raise ValueError(f"kappa must be non-negative, got {kappa!r}")
    return kappa


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

    **A period that has started has none.** Both models price the convexity
    of a rate not yet fixed at the period start, and both vanish at ``T1 =
    0``. Below zero the Ho-Lee product ``T1 T2`` went negative and so did the
    adjustment, against the promise that it never is. QuantLib's
    ``HullWhite.convexityBias`` refuses a negative ``T1``; this takes the
    limit instead and records ``period_started`` in the evidence, because a
    front contract already in its reference period is an ordinary input to
    a futures strip and refusing it would refuse the strip.

    Args:
        time_to_start: Years from valuation to the period start (``T1``);
            negative once the period is under way.
        time_to_end: Years from valuation to the period end (``T2``).
        model: ``"none"``, ``"ho_lee"`` or ``"hull_white"``.
        sigma: Normal volatility as a decimal annual rate: 1% a year, 100 bp,
            is ``0.01``. Refused above :data:`MAX_PLAUSIBLE_SIGMA`, which is
            where a sigma given in percent or in basis points lands, by the
            two models that use it; ``"none"`` ignores it.
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
        ImplausibleInputError: A time is NaN or infinite, or, for a model
            that uses them, sigma is above :data:`MAX_PLAUSIBLE_SIGMA` or NaN,
            or kappa is not finite.
        ValueError: Sigma or kappa is negative, or the period is not ordered.
    """
    if model not in _MODELS:
        raise UnsupportedConventionError(
            f"convexity model {model!r} is not implemented; "
            f"supported: {', '.join(m.value for m in ConvexityModel)}"
        )
    for name, value in (("time_to_start", time_to_start), ("time_to_end", time_to_end)):
        if not math.isfinite(value):
            raise ImplausibleInputError(f"{name} is {value!r}; a time is a finite number of years")
    if sigma < 0.0:
        raise ValueError(f"sigma must be non-negative, got {sigma!r}")
    # The band is a check on a number the model is about to use. Model
    # ``none`` uses none, and the CLI passes the config's sigma whatever the
    # model, so a leftover sigma beside ``model: none`` must not fail a
    # bootstrap that never reads it.
    if model != ConvexityModel.NONE and not sigma <= MAX_PLAUSIBLE_SIGMA:
        raise ImplausibleInputError(
            f"sigma {sigma!r} is outside the [0, {MAX_PLAUSIBLE_SIGMA}] band this build "
            f"accepts. Sigma is a decimal normal volatility a year: 1% (100 bp) is 0.01, so "
            f"{sigma!r} would be {sigma * 1e4:,.0f} bp a year. A sigma in percent or in basis "
            "points lands here; divide it by 100 or 10,000. Widen MAX_PLAUSIBLE_SIGMA "
            "deliberately if you mean it."
        )
    if model == ConvexityModel.HULL_WHITE and kappa is not None and not math.isfinite(kappa):
        raise ImplausibleInputError(f"kappa is {kappa!r}; mean reversion is a finite rate")
    if time_to_end < time_to_start:
        raise ValueError(
            f"period end {time_to_end} precedes start {time_to_start}"
        )

    started = time_to_start < 0.0
    took_limit = False
    if model == ConvexityModel.NONE or started:
        # A started period: both models are zero at T1 = 0, which is the
        # limit taken here. Hull-White still needs a valid kappa, checked
        # below, so a bad configuration does not hide behind a front month.
        adjustment = 0.0
        if model == ConvexityModel.HULL_WHITE:
            _require_kappa(kappa)
    elif model == ConvexityModel.HO_LEE:
        adjustment = 0.5 * sigma * sigma * time_to_start * time_to_end
    else:
        kappa = _require_kappa(kappa)
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
            "period_started": started,
            "sigma_units": "decimal normal volatility per year (0.01 = 100 bp)",
            # SOFR futures are a dollar product, so the collateral index is
            # SOFR by construction rather than read from a curve.
            "discounting": f"collateral_rate_ois_{SOFR.slug}",
            "discounting_note": (
                f"Collateralised flows discount on the OIS-{SOFR.label} curve because the "
                f"collateral is remunerated at {SOFR.label} (Fujii-Shimada-Takahashi; "
                "Piterbarg), not on a separate funding curve."
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
