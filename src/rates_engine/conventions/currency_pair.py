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

**Which days count.** Each currency settles on its payment system's
calendar (:data:`~rates_engine.conventions.settlement.SETTLEMENT_CALENDARS`):
the Federal Reserve's for dollars — not SIFMA's, which closes on Good Friday
and on the Friday before a Saturday holiday while Fedwire is open — and BMV
for pesos. For USD/MXN every day of the lag has to be a business day in
both, so a US holiday on T+1 delays the spot date: USD/MXN struck on
Wednesday 25 November 2026 settles on Monday the 30th, because Thanksgiving
on the 26th is not a day of the lag. That is the peso's documented exception
to the general rule for dollar pairs (:data:`JOINT_LAG_CURRENCIES` has the
sources).

The general rule counts the lag on the *other* currency's calendar only,
because no dollars move before the spot date, and then rolls the spot date
forward to a day both are open. It is QuantLib's ``FxSwapRateHelper`` with
the other currency's calendar and ``UnitedStates(FederalReserve)`` as its
trading calendar, and it applies to a dollar pair whose other currency is
not on the list. This build has no such currency yet; the rule is kept so
that adding one does not quietly put it on the peso's. A pair without the
dollar cannot be built while :class:`~rates_engine.core.money.Currency` has
two members. Its lag would count on both calendars, and ObjectLab's further
requirement that a cross also settle on a US business day is not modelled.

Two earlier versions got this wrong. The first counted every day of the lag
on a joint SIFMA and BMV calendar, and was up to three days late around
Good Friday and Saturday holidays. The second counted it on BMV alone — the
general rule — and settled 16 peso business days of 2026–2027 one to four
days early, about twenty-seven pips a day at 9.5% and 4.2%.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from rates_engine.conventions.calendar import Calendar
from rates_engine.conventions.settlement import SETTLEMENT_CALENDARS
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.money import Currency

__all__ = ["CurrencyPair", "JOINT_LAG_CURRENCIES", "USDMXN"]


JOINT_LAG_CURRENCIES: frozenset[str] = frozenset({"ARS", "CLP", "MXN"})
"""ISO codes of the currencies whose spot lag against the dollar counts US holidays.

The general rule for a dollar pair lets a US holiday fall on T+1, since no
dollars move before the spot date. ObjectLab Kit's notes on FX date
conventions (https://objectlabkit.sourceforge.net/currency.html) name three
currencies it does not apply to — the Argentine, Chilean and Mexican pesos
— for which T+1 has to be a US business day as well. Their worked example is
EUR/MXN struck on Monday 3 July 2006, which settles on Thursday the 6th
because the 4th cannot be T+1. Wikipedia's "Foreign exchange date
conventions" (https://en.wikipedia.org/wiki/Foreign_exchange_date_conventions)
gives the same Latin American exception. For a pair with one of these, every
day of the lag counts on both currencies' calendars.

Codes rather than :class:`~rates_engine.core.money.Currency` members because
two of the three are not currencies this build knows, and the list should
not have to be rediscovered when they arrive.
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
            counted as the module docstring describes. Two — the convention
            of almost every pair, USD/MXN included; USD/CAD is the common
            exception at one. Zero means the spot settles on the trade date,
            rolled forward to a day both currencies settle.
        calendars: ``(base calendar, quote calendar)``, in that order, so
            that the dollar's can be told apart. Empty — the default — means
            the two from
            :data:`~rates_engine.conventions.settlement.SETTLEMENT_CALENDARS`,
            and is replaced by them on construction, so a pair built either
            way compares equal.
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
        if calendars and len(calendars) != 2:
            raise ValueError(
                f"calendars is (base calendar, quote calendar); got {len(calendars)} of them"
            )
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

    @property
    def lag_calendars(self) -> tuple[Calendar, ...]:
        """The calendars the spot lag is counted on: each day of it is open in all of them.

        Both currencies' for USD/MXN and any pair with a currency on
        :data:`JOINT_LAG_CURRENCIES`, and for a pair without the dollar;
        the other currency's alone for any other dollar pair. See the
        module docstring.
        """
        base_calendar, quote_calendar = self.calendars
        if Currency.USD not in (self.base, self.quote):
            return self.calendars
        if self.base is Currency.USD:
            other, other_calendar = self.quote, quote_calendar
        else:
            other, other_calendar = self.base, base_calendar
        if other.value in JOINT_LAG_CURRENCIES:
            return self.calendars
        return (other_calendar,)

    def is_settlement_day(self, day: date) -> bool:
        """True when ``day`` is a business day in both currencies' calendars.

        Args:
            day: The date to check.

        Returns:
            Whether both currencies can settle on that date.
        """
        return all(calendar.is_business_day(day) for calendar in self.calendars)

    def spot_date(self, trade_date: date) -> date:
        """The date a spot trade struck on ``trade_date`` settles.

        Counts :attr:`spot_lag_days` business days forward on
        :attr:`lag_calendars`, then rolls forward to the first day both
        currencies settle. USD/MXN struck on Monday 14 September 2026 settles
        on Thursday the 17th, because the 16th is a Mexican holiday and so
        does not count; struck on Wednesday 25 November it settles on Monday
        the 30th, because Thanksgiving on the 26th is shut at the Federal
        Reserve and does not count either. The lag is counted from the trade
        date even when a calendar is shut on it — a weekend, a holiday —
        which is where this differs from QuantLib (see the comment below).

        Args:
            trade_date: The trade (valuation) date.

        Returns:
            The spot date, strictly after ``trade_date`` unless the lag is
            zero, in which case it is ``trade_date`` rolled forward to a
            settlement day.
        """
        counting = self.lag_calendars

        def counts(day: date) -> bool:
            return all(calendar.is_business_day(day) for calendar in counting)

        # Counted from the trade date itself even when it is a holiday: a
        # spot quoted on 16 September, a peso holiday on which USD/MXN still
        # trades offshore, settles two joint business days later, on the
        # 18th. QuantLib's FxSwapRateHelper first moves such a trade date to
        # the next business day of its calendar and so settles it on the
        # 21st; on every trade date both calendars are open the two agree.
        day = trade_date
        remaining = self.spot_lag_days
        while remaining:
            day += timedelta(days=1)
            if counts(day):
                remaining -= 1
        while not self.is_settlement_day(day):
            day += timedelta(days=1)
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
            "spot_lag_calendars": [calendar.name for calendar in self.lag_calendars],
        }


USDMXN = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-4, spot_lag_days=2)
"""USD/MXN: pesos per dollar, quoted to four decimals so a pip is 1e-4, spot T+2.

The pip size is the four-decimal quoting convention, which is what the pair
trades on. It is declared here rather than assumed anywhere downstream, and
it travels into every payload that reports forward points — PRD-003 AC-2.1
asks for exactly that. The spot date is two business days after the trade
on which both the Federal Reserve and BMV are open; the module docstring
has the rule and :data:`JOINT_LAG_CURRENCIES` its sources.
"""
