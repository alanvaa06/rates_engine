"""FX: the currency pair, covered interest parity, and options on it.

Separate from :mod:`rates_engine.volatility` on purpose. The rate options
there are ambiguous about their volatility units and unambiguous about
delta; FX is the other way round — the volatility is lognormal and nothing
else, and the *delta* has four conventions that give four different
strikes. Keeping the two packages apart is what stops a convention from one
being applied in the other.
"""

from rates_engine.fx import delta as delta_conventions
from rates_engine.fx import garman_kohlhagen
from rates_engine.fx.delta import (
    DeltaBasis,
    DeltaConvention,
    PremiumAdjustment,
    strike_from_delta,
)
from rates_engine.fx.quote import USDMXN, CurrencyPair

__all__ = [
    "CurrencyPair",
    "DeltaBasis",
    "DeltaConvention",
    "PremiumAdjustment",
    "USDMXN",
    "delta_conventions",
    "garman_kohlhagen",
    "strike_from_delta",
]
