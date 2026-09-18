"""Volatility: the two pricers, the units that keep them apart, and SABR.

Pure model code. Nothing here knows about curves or instruments — a pricer
takes a forward, a strike, an expiry and a volatility, and the layers above
are responsible for working out what those are.

Every refusal raised from here is defined in :mod:`rates_engine.errors` and
imported from there, never re-exported under a second name: one contract,
one place to read it.
"""

from rates_engine.volatility import bachelier, black
from rates_engine.volatility._gaussian import standard_normal_cdf, standard_normal_pdf
from rates_engine.volatility.cube import (
    CubePoint,
    CubeQuote,
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
    "StrikeConvention",
    "VolCube",
    "VolUnits",
    "Volatility",
    "bachelier",
    "black",
    "calibrate",
    "density_diagnostics",
    "expansion_is_valid",
    "standard_normal_cdf",
    "standard_normal_pdf",
]
