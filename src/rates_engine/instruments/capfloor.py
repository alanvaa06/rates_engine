"""Caps and floors: a strip of one-period options on the same index.

A cap is a sum of caplets, each a call on the forward rate of its own period,
each with its own numeraire — the period's discounted accrual. Nothing about
the strip is more than that, which is why put-call parity survives summation:
per period ``caplet - floorlet = A_i (F_i - K)``, and adding up gives the
present value of a payer swap struck at ``K``. PRD-002 AC-2.1 is that
identity, and it only holds when the cap's periods are the swap's periods, so
the test builds both from one schedule.

**Where it refuses.** A period whose forward the projection curve does not
reach raises :class:`~rates_engine.errors.MissingForwardError`. The curve
will happily extrapolate a flat forward past its last node, and for a
discount factor that is a defensible convention; for a caplet it would mean
pricing an option on a rate the market never quoted, inside a total that
reports as complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from rates_engine.conventions.calendar import SIFMA_US, BusinessDayConvention, SIFMAUSCalendar
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.schedule import Schedule
from rates_engine.errors import MissingForwardError
from rates_engine.volatility.kinds import OptionKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rates_engine.curves.discount import CurveSet

__all__ = ["Caplet", "CapFloor"]


@dataclass(frozen=True)
class Caplet:
    """One period of a cap or floor.

    Attributes:
        accrual_start: Start of the period the rate is observed over.
        accrual_end: End of it, and the rate's maturity.
        payment: When the settlement is paid.
        strike: Strike as a decimal.
        notional: Notional in USD.
        kind: ``CALL`` for a caplet, ``PUT`` for a floorlet.
        day_count: Accrual basis.
    """

    accrual_start: date
    accrual_end: date
    payment: date
    strike: float
    notional: float
    kind: OptionKind
    day_count: DayCount = DayCount.ACT_360

    @property
    def year_fraction(self) -> float:
        """Accrual length in years, on :attr:`day_count`."""
        return year_fraction(self.accrual_start, self.accrual_end, self.day_count)

    def forward_rate(self, curve_set: CurveSet) -> float:
        """The projected rate for this period, as a decimal.

        Args:
            curve_set: Discount and projection curves.

        Returns:
            The forward from the projection curve.

        Raises:
            MissingForwardError: The period ends past the projection curve's
                last node, where a forward would be extrapolated rather than
                implied.
        """
        projection = curve_set.projection
        if self.accrual_end > projection.nodes[-1]:
            raise MissingForwardError(
                f"the caplet over {self.accrual_start} to {self.accrual_end} ends past the "
                f"projection curve's last node {projection.nodes[-1]}. The curve would "
                "extrapolate a flat forward there; for a discount factor that is a "
                "convention, for an option it is a rate the market never quoted."
            )
        return projection.forward(self.accrual_start, self.accrual_end, day_count=self.day_count)

    def numeraire(self, curve_set: CurveSet) -> float:
        """The discounted accrual this caplet's payoff is scaled by, in USD."""
        return self.notional * self.year_fraction * curve_set.discount.df(self.payment)

    def describe(self) -> dict[str, Any]:
        """Terms of the caplet, for the evidence record."""
        return {
            "kind": "caplet" if self.kind is OptionKind.CALL else "floorlet",
            "accrual_start": self.accrual_start.isoformat(),
            "accrual_end": self.accrual_end.isoformat(),
            "payment": self.payment.isoformat(),
            "strike": self.strike,
            "notional": self.notional,
            "day_count": self.day_count.value,
        }


@dataclass(frozen=True)
class CapFloor:
    """A strip of caplets or floorlets on a term rate.

    Attributes:
        effective: Start of the first period.
        maturity: End of the last.
        strike: Strike as a decimal, the same for every period.
        notional: Notional in USD.
        product: ``"cap"`` or ``"floor"``.
        frequency_months: Period length, which is also the index tenor.
        day_count: Accrual basis.
        payment_lag_days: Business days from accrual end to payment.
        calendar: Calendar for payment rolls.
        convention: Roll rule.
        include_first_period: Whether the first period is an option. A cap
            written today on a rate that has already fixed has nothing
            optional about that period, and market caps omit it. Kept
            explicit because the parity in AC-2.1 needs both legs to span the
            same periods.
    """

    effective: date
    maturity: date
    strike: float
    notional: float = 1_000_000.0
    product: str = "cap"
    frequency_months: int = 3
    day_count: DayCount = DayCount.ACT_360
    payment_lag_days: int = 0
    calendar: SIFMAUSCalendar = SIFMA_US
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    include_first_period: bool = True

    @property
    def kind(self) -> OptionKind:
        """``CALL`` for a cap, ``PUT`` for a floor."""
        return OptionKind.from_cap_floor(self.product)

    @property
    def span(self) -> tuple[date, date]:
        """Effective through maturity."""
        return self.effective, self.maturity

    @property
    def schedule(self) -> Schedule:
        """The period schedule, generated by the package's one generator."""
        return Schedule.generate(
            self.effective,
            self.maturity,
            frequency_months=self.frequency_months,
            calendar=self.calendar,
            convention=self.convention,
            payment_lag_days=self.payment_lag_days,
        )

    @property
    def caplets(self) -> tuple[Caplet, ...]:
        """The periods, as individual options."""
        schedule = self.schedule
        periods = list(
            zip(schedule.accrual_start, schedule.accrual_end, schedule.payment, strict=True)
        )
        if not self.include_first_period:
            periods = periods[1:]
        return tuple(
            Caplet(
                accrual_start=start,
                accrual_end=end,
                payment=pay,
                strike=self.strike,
                notional=self.notional,
                kind=self.kind,
                day_count=self.day_count,
            )
            for start, end, pay in periods
        )

    def describe(self) -> dict[str, Any]:
        """Terms of the cap or floor, for the evidence record."""
        return {
            "kind": self.product,
            "option_kind": self.kind.value,
            "effective": self.effective.isoformat(),
            "maturity": self.maturity.isoformat(),
            "strike": self.strike,
            "notional": self.notional,
            "frequency_months": self.frequency_months,
            "day_count": self.day_count.value,
            "periods": len(self.caplets),
            "include_first_period": self.include_first_period,
        }
