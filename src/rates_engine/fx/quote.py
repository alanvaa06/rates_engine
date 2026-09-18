"""Which currency is being priced in which, and what a pip is.

An FX rate is a number with two currencies attached and an order that
matters. "18.50" is not a rate; "18.50 MXN per USD" is. Getting the order
backwards inverts every forward point, every delta and every hedge
direction, and the wrong answer looks exactly like the right one.

The vocabulary here is the market's, with the synonyms written down once
because every FX text uses a different pair of them:

``base`` / foreign
    The currency you buy one unit of. USD in USD/MXN.
``quote`` / domestic / counter
    The currency the price is expressed in. MXN in USD/MXN.

In the option formulas that makes ``r_f`` the base currency's rate and
``r_d`` the quote currency's, which is the only place the naming earns its
keep — and the place it is most often reversed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rates_engine.money import Currency

__all__ = ["CurrencyPair", "USDMXN"]


@dataclass(frozen=True)
class CurrencyPair:
    """An ordered pair, and the tick its forward points are quoted in.

    Attributes:
        base: The currency one unit of which is being priced.
        quote: The currency the price is in.
        pip: The size of one pip as a decimal of the rate. Required rather
            than defaulted: it is a quoting convention, it differs by pair,
            and a forward reported in the wrong pip is off by a factor of
            ten or a hundred with nothing to show for it.
    """

    base: Currency
    quote: Currency
    pip: float

    def __post_init__(self) -> None:
        if self.base is self.quote:
            raise ValueError(
                f"a currency pair needs two currencies, got {self.base.value} twice"
            )
        if not self.pip > 0.0:
            raise ValueError(f"pip size must be positive, got {self.pip!r}")

    @property
    def name(self) -> str:
        """``"USD/MXN"``, base first, as the market writes it."""
        return f"{self.base.value}/{self.quote.value}"

    def pips(self, rate_difference: float) -> float:
        """A difference in rate, expressed in pips.

        Args:
            rate_difference: A difference between two rates, in quote
                currency per unit of base.

        Returns:
            The same difference counted in pips.
        """
        return rate_difference / self.pip

    def to_dict(self) -> dict[str, Any]:
        """Serialise the pair with the tick that gives its points meaning."""
        return {
            "pair": self.name,
            "base": self.base.value,
            "quote": self.quote.value,
            "pip": self.pip,
        }


USDMXN = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4)
"""USD/MXN: pesos per dollar, quoted to four decimals so a pip is 1e-4.

The pip size is the four-decimal quoting convention, which is what the pair
trades on. It is declared here rather than assumed anywhere downstream, and
it travels into every payload that reports forward points — PRD-003 AC-2.1
asks for exactly that.
"""
