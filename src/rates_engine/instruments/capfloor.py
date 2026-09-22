"""Caps and floors: a strip of one-period options on the same index.

A cap is a sum of caplets, each a call on the forward rate of its own period,
each with its own numeraire — the period's discounted accrual. Nothing about
the strip is more than that, which is why put-call parity survives summation:
per period ``caplet - floorlet = A_i (F_i - K)``, and adding up gives the
present value of a payer swap struck at ``K``. PRD-002 AC-2.1 is that
identity, and it only holds when the cap's periods are the swap's periods, so
the test builds both from one schedule.

**Terms only.** The forward and the numeraire of each caplet come from
:mod:`rates_engine.pricing.options`, which is where a curve is read.

**Where it refuses.** A period whose forward the projection curve does not
reach raises :class:`~rates_engine.core.errors.MissingForwardError`. The curve
will happily extrapolate a flat forward past its last node, and for a
discount factor that is a defensible convention; for a caplet it would mean
pricing an option on a rate the market never quoted, inside a total that
reports as complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import cached_property
from typing import Any

from rates_engine.conventions.calendar import BusinessDayConvention, HolidayCalendar
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.indices import TERM_SOFR_3M, RateIndex, term_sofr
from rates_engine.conventions.option_kind import OptionKind
from rates_engine.conventions.schedule import Schedule
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.money import Currency

__all__ = ["Caplet", "CapFloor"]


@dataclass(frozen=True)
class Caplet:
    """One period of a cap or floor.

    Attributes:
        accrual_start: Start of the period the rate is observed over.
        accrual_end: End of it, and the rate's maturity.
        payment: When the settlement is paid.
        strike: Strike as a decimal.
        notional: Notional in the index's currency.
        kind: ``CALL`` for a caplet, ``PUT`` for a floorlet.
        day_count: Accrual basis.
        index: The term rate observed; it fixes the currency.
    """

    accrual_start: date
    accrual_end: date
    payment: date
    strike: float
    notional: float
    kind: OptionKind
    day_count: DayCount = DayCount.ACT_360
    index: RateIndex = TERM_SOFR_3M

    @property
    def year_fraction(self) -> float:
        """Accrual length in years, on :attr:`day_count`."""
        return year_fraction(self.accrual_start, self.accrual_end, self.day_count)

    @property
    def rate_index(self) -> RateIndex:
        """The index observed; every instrument answers this alike."""
        return self.index

    @property
    def currency(self) -> Currency:
        """The settlement currency, from :attr:`index`."""
        return self.index.currency

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
        notional: Notional in the index's currency.
        product: ``"cap"`` or ``"floor"``.
        frequency_months: Period length, which is also the index tenor.
            ``None`` takes it from :attr:`index`; with neither, three months.
        day_count: Accrual basis.
        payment_lag_days: Business days from accrual end to payment.
        calendar: Calendar for payment rolls; ``None`` rolls on the index's.
        convention: Roll rule.
        include_first_period: Whether the first period is an option. A cap
            written today on a rate that has already fixed has nothing
            optional about that period, and market caps omit it. Kept
            explicit because the parity in AC-2.1 needs both legs to span the
            same periods.
        index: The term rate every caplet observes; ``None`` means Term SOFR
            of :attr:`frequency_months`.
    """

    effective: date
    maturity: date
    strike: float
    notional: float = 1_000_000.0
    product: str = "cap"
    frequency_months: int | None = None
    day_count: DayCount = DayCount.ACT_360
    payment_lag_days: int = 0
    calendar: HolidayCalendar | None = None
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    include_first_period: bool = True
    index: RateIndex | None = None

    def __post_init__(self) -> None:
        index = self.rate_index
        if index.tenor.months is None:
            raise UnsupportedConventionError(
                f"a CapFloor schedules its periods in whole months; {index.name} "
                f"accrues {index.tenor.value} periods and has no instrument here yet"
            )
        if self.frequency_months is not None and self.frequency_months != index.tenor.months:
            raise UnsupportedConventionError(
                f"frequency_months={self.frequency_months} disagrees with "
                f"{index.name}, a {index.tenor.months}-month rate. Set both together, "
                "or leave frequency_months unset to take it from the index."
            )
        # Resolved into both fields, so that the three spellings of one
        # trade -- neither given, the frequency, or the index -- are equal and
        # hash alike. Changing one later with ``replace`` must change both.
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "frequency_months", index.tenor.months)

    @property
    def rate_index(self) -> RateIndex:
        """The index each caplet observes: :attr:`index`, else Term SOFR of the frequency."""
        if self.index is not None:
            return self.index
        return term_sofr(self.frequency_months or 3)

    @property
    def period_months(self) -> int:
        """Period length in months: the index tenor."""
        months = self.rate_index.tenor.months
        assert months is not None  # enforced in __post_init__
        return months

    @property
    def currency(self) -> Currency:
        """The settlement currency, from the index."""
        return self.rate_index.currency

    @property
    def kind(self) -> OptionKind:
        """``CALL`` for a cap, ``PUT`` for a floor."""
        return OptionKind.from_cap_floor(self.product)

    @property
    def span(self) -> tuple[date, date]:
        """Effective through maturity."""
        return self.effective, self.maturity

    @cached_property
    def schedule(self) -> Schedule:
        """The period schedule, generated by the package's one generator."""
        return Schedule.generate(
            self.effective,
            self.maturity,
            frequency_months=self.period_months,
            calendar=self.calendar if self.calendar is not None else self.rate_index.calendar,
            convention=self.convention,
            payment_lag_days=self.payment_lag_days,
        )

    @cached_property
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
                index=self.rate_index,
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
            "frequency_months": self.period_months,
            "day_count": self.day_count.value,
            "periods": len(self.caplets),
            "include_first_period": self.include_first_period,
        }
