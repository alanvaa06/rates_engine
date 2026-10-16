"""One dated amount, the accrual that produced it, and the history it rests on."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rates_engine.core.evidence import Provenance
from rates_engine.core.money import Currency

__all__ = ["Cashflow", "RealisedFixing"]


@dataclass(frozen=True)
class RealisedFixing:
    """The published fixings behind the part of a floating coupon that was already history.

    A floating period that began before the valuation date is part fixed
    and part forecast. The fixed part comes from rates someone published,
    and a number resting on them is only as good as they are, so the record
    of which ones were read travels with the flow and reaches the
    valuation's evidence.

    Attributes:
        series_id: The series read: the index's name, e.g. ``"SOFR"``.
        method: ``"compounded"`` for an overnight rate compounded in arrears
            over ``[observed_from, observed_to)``; ``"term"`` for a term rate
            fixed once, on ``observed_from``.
        observed_from: First fixing date the record covers.
        observed_to: End of the compounding window, exclusive, for
            ``"compounded"``; the fixing date itself for ``"term"``.
        rate: The realised rate as a decimal: compounded over the window on
            ACT/360, or the term fixing as published.
        accrual_factor: Compounded growth over the window, ``1`` plus the
            simple rate times its ACT/360 year fraction; ``None`` for a term
            fixing, which has no window.
        business_days: Published fixings read.
        repeated_days: Calendar days that reused the previous published rate
            because none was published for them.
        provenance: Where the series came from, its data quality included.
    """

    series_id: str
    method: str
    observed_from: date
    observed_to: date
    rate: float
    accrual_factor: float | None
    business_days: int
    repeated_days: int
    provenance: Provenance

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-ready mapping, ``None`` never a missing key."""
        return {
            "series_id": self.series_id,
            "method": self.method,
            "observed_from": self.observed_from.isoformat(),
            "observed_to": self.observed_to.isoformat(),
            "rate": self.rate,
            "accrual_factor": self.accrual_factor,
            "business_days": self.business_days,
            "repeated_days": self.repeated_days,
            "source": self.provenance.source,
            "data_quality": self.provenance.data_quality.value,
        }


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
        fixing: The published fixings behind the part of a floating amount
            that was history on the valuation date, or ``None`` when no part
            of it was. The valuation's evidence reports it, as
            ``realised_fixings``, rather than :meth:`to_dict`: evidence is
            where this package says what a number rests on, and a flow's own
            payload keeps one shape whether or not the trade has started.
    """

    payment_date: date
    amount: float
    leg: str
    accrual_start: date
    accrual_end: date
    year_fraction: float
    rate: float | None = None
    currency: Currency = Currency.USD
    fixing: RealisedFixing | None = None

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
