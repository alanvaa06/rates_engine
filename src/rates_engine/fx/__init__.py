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
from rates_engine.fx.forward import FXForwardResult, forward_from_curves, implied_basis
from rates_engine.fx.quote import USDMXN, CurrencyPair
from rates_engine.fx.vannavolga import (
    ATMConvention,
    SmileQuotes,
    SmileReading,
    VannaVolgaSmile,
)

__all__ = [
    "ATMConvention",
    "CurrencyPair",
    "FXForwardResult",
    "DeltaBasis",
    "DeltaConvention",
    "PremiumAdjustment",
    "SmileQuotes",
    "SmileReading",
    "USDMXN",
    "VannaVolgaSmile",
    "delta_conventions",
    "forward_from_curves",
    "implied_basis",
    "garman_kohlhagen",
    "strike_from_delta",
]
