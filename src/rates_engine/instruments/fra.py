"""A forward rate agreement, and the reason its fair rate is not the discount forward.

In a single-curve world the fair FRA rate is the forward implied by the curve
you discount with, and the two questions have one answer. With a basis they
separate: the rate is projected off the tenor curve and the settlement is
discounted on OIS, so the fair rate follows the projection curve and ignores
the discount curve entirely for a single-period contract. ``test_pricing.py``
exercises both branches — with a basis the FRA rate differs from the discount
curve's forward, and with the basis set to zero the two coincide — because
a plumbing error here looks like a small pricing difference rather than a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.indices import TERM_SOFR_3M, IndexTenor, RateIndex
from rates_engine.conventions.side import Side
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.money import Currency

__all__ = ["FRA"]


@dataclass(frozen=True)
class FRA:
    """A single-period forward rate agreement settled at the period end.

    Terms only. The fair rate and the settlement flow come from
    :mod:`rates_engine.pricing.projection` (``fra_fair_rate``, ``project``).

    Attributes:
        start: Start of the reference period.
        end: End of it.
        rate: Contract rate as a decimal.
        notional: Notional in the index's currency.
        side: ``"payer"`` pays the fixed rate and receives the index.
        day_count: Accrual basis for the period.
        index: The term rate the contract settles against; it fixes the
            currency. Term SOFR 3M by default, whatever the period length:
            a FRA has no frequency to take a tenor from, so name the index
            when the period is not three months.

    Raises:
        UnsupportedConventionError: ``index`` is an overnight rate.
    """

    start: date
    end: date
    rate: float
    notional: float = 1_000_000.0
    side: str = Side.PAYER
    day_count: DayCount = DayCount.ACT_360
    index: RateIndex = TERM_SOFR_3M

    def __post_init__(self) -> None:
        if self.index.tenor is IndexTenor.OVERNIGHT:
            raise UnsupportedConventionError(
                f"a FRA settles against a term rate known at the period start; "
                f"{self.index.name} is an overnight rate, compounded in arrears. "
                "An overnight period is an OISSwap of one period."
            )

    @property
    def rate_index(self) -> RateIndex:
        """The index settled against; every instrument answers this alike."""
        return self.index

    @property
    def currency(self) -> Currency:
        """The settlement currency, from :attr:`index`."""
        return self.index.currency

    @property
    def span(self) -> tuple[date, date]:
        """Reference period start and end, as a half-open interval."""
        return self.start, self.end

    @property
    def year_fraction(self) -> float:
        """Reference period length in years, on :attr:`day_count`."""
        return year_fraction(self.start, self.end, self.day_count)

    def describe(self) -> dict[str, object]:
        """Terms of the FRA, for the evidence record."""
        return {
            "kind": "fra",
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rate": self.rate,
            "notional": self.notional,
            "side": self.side,
            "day_count": self.day_count.value,
            "index": self.index.name,
        }
