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
from typing import TYPE_CHECKING

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.instruments.swaps import Side, _sign

if TYPE_CHECKING:  # pragma: no cover - import for typing only, avoids a cycle
    from rates_engine.curves.discount import CurveSet

__all__ = ["FRA"]


@dataclass(frozen=True)
class FRA:
    """A single-period forward rate agreement settled at the period end.

    Attributes:
        start: Start of the reference period.
        end: End of it.
        rate: Contract rate as a decimal.
        notional: Notional in USD.
        side: ``"payer"`` pays the fixed rate and receives the index.
        day_count: Accrual basis for the period.
    """

    start: date
    end: date
    rate: float
    notional: float = 1_000_000.0
    side: str = Side.PAYER
    day_count: DayCount = DayCount.ACT_360

    @property
    def span(self) -> tuple[date, date]:
        """Reference period start and end, as a half-open interval."""
        return self.start, self.end

    @property
    def year_fraction(self) -> float:
        """Reference period length in years, on :attr:`day_count`."""
        return year_fraction(self.start, self.end, self.day_count)

    def fair_rate(self, curve_set: CurveSet) -> float:
        """The rate that makes the contract worth zero, from the projection curve.

        Args:
            curve_set: Discount and projection curves.

        Returns:
            The fair rate as a decimal.
        """
        return curve_set.projection.forward(self.start, self.end, day_count=self.day_count)

    def cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The single settlement, signed for :attr:`side`."""
        sign = -_sign(self.side)
        projected = self.fair_rate(curve_set)
        tau = self.year_fraction
        return (
            Cashflow(
                payment_date=self.end,
                amount=sign * self.notional * (projected - self.rate) * tau,
                leg="float",
                accrual_start=self.start,
                accrual_end=self.end,
                year_fraction=tau,
                rate=projected,
            ),
        )

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
        }
