"""Volatility: the two pricers, the units that keep them apart, and SABR.

Pure model code. Nothing here knows about curves or instruments — a pricer
takes a forward, a strike, an expiry and a volatility, and the layers above
are responsible for working out what those are.
"""

from rates_engine.volatility import bachelier, black
from rates_engine.volatility.cube import (
    CubePoint,
    CubeQuote,
    SliceNotQuotedError,
    StrikeConvention,
    VolCube,
)
from rates_engine.volatility.kinds import OptionKind
from rates_engine.volatility.sabr import (
    DEFAULT_BETA,
    SABRCalibration,
    SABRParameters,
    calibrate,
    density_diagnostics,
    expansion_is_valid,
)
from rates_engine.volatility.units import Volatility, VolUnits

__all__ = [
    "CubePoint",
    "CubeQuote",
    "DEFAULT_BETA",
    "OptionKind",
    "SABRCalibration",
    "SABRParameters",
    "SliceNotQuotedError",
    "StrikeConvention",
    "VolCube",
    "VolUnits",
    "Volatility",
    "bachelier",
    "black",
    "calibrate",
    "density_diagnostics",
    "expansion_is_valid",
]
