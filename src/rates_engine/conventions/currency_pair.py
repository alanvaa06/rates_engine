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

**A spot trade does not settle today.** A quoted spot rate is the price for
delivery on the *spot date*, two business days after the trade for USD/MXN,
and a forward's points run from that date rather than from the trade date.
Treating the spot as if it settled on the valuation date carries both
currencies for two extra days: about seventy-six pips on USD/MXN at any
tenor, and eighty-two when a Mexican holiday pushes the spot date out to
T+3. The lag and the calendars are therefore properties of the pair, next
to the pip, rather than something each pricer remembers.

The calendar is the *joint* one: a day counts towards the lag only when it
is a business day in every calendar of the pair. That is QuantLib's
``JointCalendar(UnitedStates, Mexico)`` advanced by two days, which is what
its ``FxSwapRateHelper`` uses with two fixing days. Two simplifications are
recorded here rather than hidden. The US calendar is ``SIFMA_US``, the bond
market's, because it is the only US calendar in this package; the banks'
calendar (the Federal Reserve's) differs from it on Good Friday — where
Mexico is closed anyway — and on holidays that fall on a Saturday, where
SIFMA usually closes the Friday and the banks do not. And the market rule
for a dollar pair is slightly looser than a joint calendar: a US holiday on
T+1 alone does not push the spot date, because no dollars move that day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from rates_engine.conventions.calendar import BMV, SIFMA_US, Calendar
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.money import Currency

__all__ = ["CurrencyPair", "USDMXN", "SETTLEMENT_CALENDARS"]

SETTLEMENT_CALENDARS: dict[Currency, Calendar] = {
    Currency.USD: SIFMA_US,
    Currency.MXN: BMV,
}
"""The calendar each currency settles on, used when a pair names none.

``SIFMA_US`` for dollars and ``BMV`` for pesos, because they are the two
calendars this package has. Neither is the banking calendar FX actually
settles on — the Federal Reserve's and Banxico's — and the module docstring
says where they differ. A currency missing from this table makes
:meth:`CurrencyPair.spot_date` refuse rather than settle on a calendar
nobody chose.
"""


@dataclass(frozen=True)
class CurrencyPair:
    """An ordered pair, the tick its forward points are quoted in, and its spot date.

    Attributes:
        base: The currency one unit of which is being priced.
        quote: The currency the price is in.
        pip: The size of one pip as a decimal of the rate. Required rather
            than defaulted: it is a quoting convention, it differs by pair,
            and a forward reported in the wrong pip is off by a factor of
            ten or a hundred with nothing to show for it.
        spot_lag_days: Business days from the trade date to the spot date,
            counted on the joint calendar. Two — the convention of almost
            every pair, USD/MXN included; USD/CAD is the common exception
            at one. Zero means the spot settles on the trade date, rolled
            forward to a business day.
        calendars: The settlement calendars, every one of which must be open
            on a day for it to count. Empty — the default — means one per
            currency from :data:`SETTLEMENT_CALENDARS`, and is replaced by
            that tuple on construction, so a pair built either way compares
            equal.
    """

    base: Currency
    quote: Currency
    pip: float
    spot_lag_days: int = 2
    calendars: tuple[Calendar, ...] = ()

    def __post_init__(self) -> None:
        if self.base is self.quote:
            raise ValueError(
                f"a currency pair needs two currencies, got {self.base.value} twice"
            )
        if not self.pip > 0.0:
            raise ValueError(f"pip size must be positive, got {self.pip!r}")
        if (
            isinstance(self.spot_lag_days, bool)
            or not isinstance(self.spot_lag_days, int)
            or self.spot_lag_days < 0
        ):
            raise ValueError(
                f"spot_lag_days is a whole number of business days, zero or more; got "
                f"{self.spot_lag_days!r}"
            )
        calendars = tuple(self.calendars)
        if not calendars:
            missing = [c.value for c in (self.base, self.quote) if c not in SETTLEMENT_CALENDARS]
            if missing:
                raise UnsupportedConventionError(
                    f"{', '.join(missing)} has no settlement calendar in "
                    "SETTLEMENT_CALENDARS, so this pair has no spot date. Pass the "
                    "calendars explicitly, or add the currency to the table."
                )
            calendars = (SETTLEMENT_CALENDARS[self.base], SETTLEMENT_CALENDARS[self.quote])
        object.__setattr__(self, "calendars", calendars)

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

    def is_settlement_day(self, day: date) -> bool:
        """True when ``day`` is a business day in every calendar of the pair.

        Args:
            day: The date to check.

        Returns:
            Whether both currencies can settle on that date.
        """
        return all(calendar.is_business_day(day) for calendar in self.calendars)

    def spot_date(self, trade_date: date) -> date:
        """The date a spot trade struck on ``trade_date`` settles.

        Counts :attr:`spot_lag_days` settlement days forward on the joint
        calendar, so a holiday in either currency inside the lag pushes the
        spot date out: USD/MXN struck on Monday 14 September 2026 settles on
        Thursday the 17th, because the 16th is a Mexican holiday. A trade
        date that is itself a holiday counts from the next settlement day,
        which is how QuantLib's ``Calendar.advance`` counts.

        Args:
            trade_date: The trade (valuation) date.

        Returns:
            The spot date, strictly after ``trade_date`` unless the lag is
            zero, in which case it is ``trade_date`` rolled forward to a
            settlement day.
        """
        day = trade_date
        if self.spot_lag_days == 0:
            while not self.is_settlement_day(day):
                day += timedelta(days=1)
            return day
        remaining = self.spot_lag_days
        while remaining:
            day += timedelta(days=1)
            if self.is_settlement_day(day):
                remaining -= 1
        return day

    def to_dict(self) -> dict[str, Any]:
        """Serialise the pair with the tick and the spot date that give its points meaning."""
        return {
            "pair": self.name,
            "base": self.base.value,
            "quote": self.quote.value,
            "pip": self.pip,
            "spot_lag_days": self.spot_lag_days,
            "settlement_calendars": [calendar.name for calendar in self.calendars],
        }


USDMXN = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=2)
"""USD/MXN: pesos per dollar, quoted to four decimals so a pip is 1e-4, spot T+2.

The pip size is the four-decimal quoting convention, which is what the pair
trades on. It is declared here rather than assumed anywhere downstream, and
it travels into every payload that reports forward points — PRD-003 AC-2.1
asks for exactly that. The spot date is two business days after the trade
on the joint ``SIFMA_US`` and ``BMV`` calendar; the module docstring says
how that differs from the banking calendars the pair really settles on.
"""
