"""One dated amount, and the accrual that produced it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rates_engine.money import Currency

__all__ = ["Cashflow"]


@dataclass(frozen=True)
class Cashflow:
    """A single payment, with enough context to audit where it came from.

    Attributes:
        payment_date: When it is paid.
        amount: Signed amount, from the holder's point of view, in
            :attr:`currency`.
        leg: ``"fixed"`` or ``"float"``.
        accrual_start: Start of the period it accrued over.
        accrual_end: End of that period.
        year_fraction: Length of that period in years, on the leg's own basis.
        rate: The rate applied, as a decimal. ``None`` for a notional exchange.
        currency: What :attr:`amount` is denominated in. Defaults to USD,
            which is what every flow in v1 and v2 already was.
    """

    payment_date: date
    amount: float
    leg: str
    accrual_start: date
    accrual_end: date
    year_fraction: float
    rate: float | None = None
    currency: Currency = Currency.USD

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-ready mapping, ``None`` never a missing key."""
        return {
            "payment_date": self.payment_date.isoformat(),
            "amount": self.amount,
            "leg": self.leg,
            "currency": self.currency.value,
            "accrual_start": self.accrual_start.isoformat(),
            "accrual_end": self.accrual_end.isoformat(),
            "year_fraction": self.year_fraction,
            "rate": self.rate,
        }
