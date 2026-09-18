"""European swaptions: the option, and the two curve facts that price it.

A swaption is an option to enter a swap, so everything about it reduces to
two numbers taken off the curve — the forward swap rate and the annuity — and
then a one-dimensional option formula.

**Those two numbers are not here.** They are pricing facts about the
underlying, and computing them needs ``par_rate`` and ``annuity``, which live
a layer above instruments. Reaching up for them would make the module graph
cyclic — instruments importing pricing importing instruments — so they live
in :mod:`rates_engine.optionpricing` as
:func:`~rates_engine.optionpricing.forward_swap_rate` and
:func:`~rates_engine.optionpricing.swaption_annuity`. What stays here is the
contract: dates, strike, side, and the option's own expiry.

**Why the annuity is the numeraire.** Under the annuity measure the forward
swap rate is a martingale, which is what makes Black and Bachelier applicable
to a swaption at all rather than merely convenient. The annuity is not a
discount factor bolted on at the end; it is the change of numeraire that
makes the formula true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.discount import CURVE_TIME_BASIS
from rates_engine.instruments.side import Side
from rates_engine.instruments.swaps import IRSwap, OISSwap
from rates_engine.volatility.kinds import OptionKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = ["Swaption"]


@dataclass(frozen=True)
class Swaption:
    """A European option to enter a swap at a fixed rate.

    Attributes:
        expiry: Option expiry date. The underlying swap starts on or after it.
        underlying: The swap entered on exercise. Its ``fixed_rate`` is
            ignored — :attr:`strike` is the rate exercised at — and its
            ``side`` is ignored in favour of :attr:`side`.
        strike: The fixed rate the option is struck at, as a decimal.
        side: ``"payer"`` for the right to pay fixed, ``"receiver"`` for the
            right to receive it.
        notional: Notional in USD.
    """

    expiry: date
    underlying: OISSwap | IRSwap
    strike: float
    side: str = Side.PAYER
    notional: float = 1_000_000.0

    def __post_init__(self) -> None:
        if self.side not in (Side.PAYER, Side.RECEIVER):
            raise ValueError(
                f"side must be '{Side.PAYER}' or '{Side.RECEIVER}', got {self.side!r}"
            )
        if self.expiry > self.underlying.effective:
            raise ValueError(
                f"the option expires at {self.expiry}, after the underlying swap starts "
                f"at {self.underlying.effective}; a swaption cannot be exercised into a "
                "swap that has already begun accruing"
            )

    @property
    def kind(self) -> OptionKind:
        """``CALL`` for a payer, ``PUT`` for a receiver."""
        return OptionKind.from_swaption_side(self.side)

    @property
    def span(self) -> tuple[date, date]:
        """Option expiry through the underlying's maturity."""
        return self.expiry, self.underlying.maturity

    def time_to_expiry(self, as_of: date, *, day_count: DayCount = CURVE_TIME_BASIS) -> float:
        """Years from a valuation date to expiry.

        Args:
            as_of: Valuation date.
            day_count: Basis for the year fraction. The curve's own basis by
                default, so option time and curve time agree.

        Returns:
            The time in years, zero at or after expiry.
        """
        return max(year_fraction(as_of, self.expiry, day_count), 0.0)

    def describe(self) -> dict[str, Any]:
        """Terms of the swaption, for the evidence record."""
        return {
            "kind": "swaption",
            "expiry": self.expiry.isoformat(),
            "side": self.side,
            "option_kind": self.kind.value,
            "strike": self.strike,
            "notional": self.notional,
            "underlying": self.underlying.describe(),
        }
