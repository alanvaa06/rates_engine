"""The volatility cube: expiry by tenor by strike, and what fills the gaps.

A swaption market quotes a grid — a handful of expiries, a handful of tenors,
and for each pair a few strikes around the forward. Everything else has to
come from a model, and the model used here is SABR fitted to the smile of
that expiry and tenor, which is what PRD-002 AC-3.1 asks for.

**What it interpolates and what it refuses.** Inside a quoted
``(expiry, tenor)`` slice, an unquoted strike is filled by SABR. Across
slices it refuses: interpolating volatility between expiries or tenors is a
modelling choice with its own literature, and picking one silently would put
an unexamined assumption under every price. v2 fills strikes; a surface
model is a later question.

**Beyond the quoted strikes it extrapolates, and says so.** A strike below
the lowest quote or above the highest is still answered by the fitted smile,
because a cap or a far-wing swaption needs a number, but nothing in the data
constrains it there: the 1y5y fixture, quoted from 3.25% to 5.25%, gives
248.5 bp at 20%. Such a reading is ``source="sabr_extrapolated"``,
``reliable=False``, and carries an ``outside_quoted_strikes`` degradation that
marks it ``assumed``. It used to come back as ``"sabr"`` with a provenance
note calling it interpolated.

**Units travel with the numbers.** A cube that does not say whether it holds
normal basis points or lognormal percent, and whether its strikes are
absolute or offsets from the forward, is four ways ambiguous. All of it is
carried on the cube and serialised with it (AC-3.4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from rates_engine.core.errors import SliceNotQuotedError, VolatilityError
from rates_engine.core.evidence import DataQuality, Degradation, Evidence, Provenance
from rates_engine.core.results import EngineResult
from rates_engine.models.sabr import DEFAULT_BETA, SABRCalibration, calibrate, normal_vol
from rates_engine.volatility.units import Volatility, VolUnits

__all__ = ["StrikeConvention", "CubePoint", "VolCube", "CubeQuote", "STRIKE_TOLERANCE"]

STRIKE_TOLERANCE = 1e-10
"""Two strikes closer than this, in decimal rate, are one strike: a millionth of a bp.

A strike asked for as ``forward + offset`` and the one a quote was written at
differ by a rounding error -- up to 6.9e-18 on the test fixture -- and
comparing them with ``==`` called 8 of its 27 quoted points unquoted,
refitted them, and reported a SABR number in place of the quote. This is
ten orders of magnitude above that noise and far below any spacing a market
quotes strikes at.
"""


class StrikeConvention(StrEnum):
    """How a cube's strikes are written down.

    ``ABSOLUTE``
        The strike is a rate, so ``0.045`` is 4.5%.
    ``RELATIVE_TO_FORWARD``
        The strike is an offset from the forward in decimal rate, so ``0.005``
        is fifty basis points above the money and ``0.0`` is at it.
    """

    ABSOLUTE = "absolute"
    RELATIVE_TO_FORWARD = "relative_to_forward"


@dataclass(frozen=True)
class CubePoint:
    """One quoted volatility.

    Attributes:
        expiry: Option expiry in years.
        tenor: Underlying swap tenor in years.
        strike: Strike, read under the cube's :class:`StrikeConvention`.
        volatility: The quote, carrying its own units.
    """

    expiry: float
    tenor: float
    strike: float
    volatility: Volatility

    def to_dict(self) -> dict[str, Any]:
        """Serialise the point and the units of its quote."""
        return {
            "expiry": self.expiry,
            "tenor": self.tenor,
            "strike": self.strike,
            "volatility": self.volatility.to_dict(),
        }


@dataclass(frozen=True)
class CubeQuote(EngineResult):
    """A volatility read out of the cube, and where it came from.

    Attributes:
        volatility: The answer, in normal basis points.
        expiry: Expiry asked for, in years.
        tenor: Tenor asked for, in years.
        strike: Absolute strike asked for, as a decimal.
        source: ``"quoted"`` when the grid held it, ``"sabr"`` when the smile
            was fitted to produce it between quoted strikes, and
            ``"sabr_extrapolated"`` when the strike lies outside them.
        calibration: The fit behind a SABR answer, else ``None``.
        reliable: ``False`` for an extrapolation, which no quote constrains;
            ``True`` for a quote or a fill between quotes.
    """

    volatility: Volatility
    expiry: float
    tenor: float
    strike: float
    source: str
    calibration: SABRCalibration | None
    reliable: bool = True

    def payload_fields(self) -> dict[str, Any]:
        """The quote, the point asked for, and the fit if there was one."""
        return {
            "volatility": self.volatility.to_dict(),
            "expiry": self.expiry,
            "tenor": self.tenor,
            "strike": self.strike,
            "source": self.source,
            "reliable": self.reliable,
            "calibration": self.calibration.payload_fields() if self.calibration else None,
        }


@dataclass(frozen=True)
class VolCube:
    """Quoted swaption volatilities, with SABR filling strikes inside a smile.

    Attributes:
        as_of: The date the quotes are from. A cube without one is a set of
            numbers, not a market.
        points: The quotes.
        units: The quoting convention every point must share.
        strike_convention: How the strikes are written.
        beta: The SABR backbone exponent used when a smile is fitted. Fixed
            rather than calibrated; see :mod:`rates_engine.models.sabr`.
        shift: Shift applied when fitting, for slices whose forward is at or
            below zero.
        provenance: Where the quotes came from.
    """

    as_of: date
    points: tuple[CubePoint, ...]
    units: VolUnits = VolUnits.NORMAL_BP
    strike_convention: StrikeConvention = StrikeConvention.ABSOLUTE
    beta: float = DEFAULT_BETA
    shift: float = 0.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="file", instrument_kind="swaption_vol")
    )

    def __post_init__(self) -> None:
        mismatched = [p for p in self.points if p.volatility.units is not self.units]
        if mismatched:
            raise VolatilityError(
                f"the cube declares {self.units.value} but {len(mismatched)} point(s) are "
                f"quoted otherwise, first at expiry {mismatched[0].expiry} tenor "
                f"{mismatched[0].tenor} strike {mismatched[0].strike}. A cube with mixed "
                "units is four numbers pretending to be one surface."
            )
        if not self.units.is_normal:
            raise VolatilityError(
                f"the cube holds {self.units.value}; v2 stores normal volatility, which is "
                "what the swaption market quotes. Convert at the boundary, deliberately."
            )

    @property
    def slices(self) -> dict[tuple[float, float], tuple[CubePoint, ...]]:
        """Points grouped by ``(expiry, tenor)``, each sorted by strike."""
        grouped: dict[tuple[float, float], list[CubePoint]] = {}
        for point in self.points:
            grouped.setdefault((point.expiry, point.tenor), []).append(point)
        return {
            key: tuple(sorted(value, key=lambda p: p.strike))
            for key, value in sorted(grouped.items())
        }

    def absolute_strike(self, strike: float, forward: float) -> float:
        """Convert a cube strike into an absolute rate.

        Args:
            strike: The strike as the cube writes it.
            forward: The forward for this slice, as a decimal.

        Returns:
            The absolute strike as a decimal.
        """
        if self.strike_convention is StrikeConvention.ABSOLUTE:
            return strike
        return forward + strike

    def calibrate_slice(
        self, expiry: float, tenor: float, forward: float
    ) -> SABRCalibration:
        """Fit SABR to one quoted smile.

        Args:
            expiry: Expiry in years, matching a quoted slice exactly.
            tenor: Tenor in years, matching a quoted slice exactly.
            forward: The forward swap rate for this slice, as a decimal.

        Returns:
            The :class:`~rates_engine.models.sabr.SABRCalibration`.

        Raises:
            SliceNotQuotedError: No quotes exist for this expiry and tenor.
            CalibrationError: The slice has fewer than three strikes.
        """
        points = self.slices.get((expiry, tenor))
        if not points:
            raise SliceNotQuotedError(
                f"the cube has no quotes at expiry {expiry} tenor {tenor}; it holds "
                f"{sorted(self.slices)}. Strikes are filled by SABR inside a quoted "
                "smile, and a missing slice is not a gap in a smile, it is a missing "
                "smile. v2 does not interpolate across expiry or tenor."
            )
        return calibrate(
            forward,
            expiry,
            tuple(self.absolute_strike(p.strike, forward) for p in points),
            tuple(p.volatility.as_normal_decimal() for p in points),
            beta=self.beta,
            shift=self.shift,
        )

    def volatility_at(
        self, expiry: float, tenor: float, strike: float, forward: float
    ) -> CubeQuote:
        """Read a volatility, quoted if the grid has it and fitted if not.

        Args:
            expiry: Expiry in years.
            tenor: Tenor in years.
            strike: Absolute strike as a decimal.
            forward: The forward swap rate for this slice, as a decimal.

        Returns:
            The :class:`CubeQuote`, carrying which of the three it was: a
            quote (matched within :data:`STRIKE_TOLERANCE`), a SABR fill
            between quoted strikes, or a SABR extrapolation beyond them,
            marked unreliable and warned.

        Raises:
            SliceNotQuotedError: No quotes exist for this expiry and tenor.
            CalibrationError: A fit was needed and the slice is too thin.
            ExpansionBreakdownError: The fitted smile has no usable
                volatility at this strike.
        """
        points = self.slices.get((expiry, tenor))
        if not points:
            raise SliceNotQuotedError(
                f"the cube has no quotes at expiry {expiry} tenor {tenor}; it holds "
                f"{sorted(self.slices)}"
            )
        quoted = [(self.absolute_strike(p.strike, forward), p) for p in points]
        for absolute, point in quoted:
            if math.isclose(absolute, strike, rel_tol=0.0, abs_tol=STRIKE_TOLERANCE):
                return CubeQuote(
                    evidence=Evidence(
                        produced_by="volatility.cube.volatility_at",
                        inputs=(self.provenance,),
                        fields={
                            "source": "quoted",
                            "as_of": self.as_of.isoformat(),
                            "units": self.units.value,
                            "strike_convention": self.strike_convention.value,
                            "expiry": expiry,
                            "tenor": tenor,
                            "strike": strike,
                        },
                    ),
                    volatility=point.volatility,
                    expiry=expiry,
                    tenor=tenor,
                    strike=strike,
                    source="quoted",
                    calibration=None,
                )

        # The quoted strikes are sorted, and both conventions map them to
        # absolute strikes monotonically, so the ends are the range.
        lowest, highest = quoted[0][0], quoted[-1][0]
        extrapolated = not lowest < strike < highest
        fit = self.calibrate_slice(expiry, tenor, forward)
        value = normal_vol(fit.parameters, forward, strike, expiry)
        warnings: tuple[Degradation, ...] = ()
        if extrapolated:
            warnings = (
                Degradation(
                    code="outside_quoted_strikes",
                    message=(
                        f"strike {strike} is outside the quoted strikes of the "
                        f"{expiry}x{tenor} smile, {lowest} to {highest}; the volatility is "
                        "SABR extrapolated beyond its data, which no quote constrains."
                    ),
                    data_quality=DataQuality.ASSUMED,
                ),
            )
        notes = (
            "Extrapolated by SABR beyond the quoted strikes of this smile."
            if extrapolated
            else "Interpolated by SABR from the quoted strikes of this smile."
        )
        source = "sabr_extrapolated" if extrapolated else "sabr"
        return CubeQuote(
            evidence=Evidence(
                produced_by="volatility.cube.volatility_at",
                inputs=(
                    Provenance(
                        source=self.provenance.source,
                        series_id=f"smile:{expiry}x{tenor}",
                        instrument_kind="swaption_vol",
                        data_quality=DataQuality.SYNTHETIC,
                        notes=notes,
                    ),
                ),
                fields={
                    "source": source,
                    "as_of": self.as_of.isoformat(),
                    "units": self.units.value,
                    "strike_convention": self.strike_convention.value,
                    "expiry": expiry,
                    "tenor": tenor,
                    "strike": strike,
                    "forward": forward,
                    "extrapolated": extrapolated,
                    "quoted_strike_range": [lowest, highest],
                    "quoted_strikes_used": list(fit.strikes),
                    "parameters": fit.parameters.to_dict(),
                    "rmse_bp": fit.rmse_bp,
                },
                warnings=warnings,
                sources=(fit.evidence,),
            ),
            volatility=Volatility(value * 1e4, VolUnits.NORMAL_BP),
            expiry=expiry,
            tenor=tenor,
            strike=strike,
            source=source,
            calibration=fit,
            reliable=not extrapolated,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the cube with every convention that gives it meaning (AC-3.4)."""
        return {
            "as_of": self.as_of.isoformat(),
            "units": self.units.value,
            "strike_convention": self.strike_convention.value,
            "beta": self.beta,
            "shift": self.shift,
            "slices": [
                {
                    "expiry": expiry,
                    "tenor": tenor,
                    "strikes": [p.strike for p in points],
                    "volatilities": [p.volatility.value for p in points],
                }
                for (expiry, tenor), points in self.slices.items()
            ],
            "points": len(self.points),
            "provenance": self.provenance.to_dict(),
        }
