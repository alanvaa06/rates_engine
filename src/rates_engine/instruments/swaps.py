"""OIS and IRS: a fixed leg, a floating leg, and the index each one floats on.

The difference between the two products is one line of code and the whole
post-LIBOR argument. An OIS floats on compounded overnight, which *is* the
discount curve's own forward, so one curve answers both questions. An IRS
floats on a term rate whose forward lives on a different curve, and only the
discounting stays on OIS. Collapse the two curves and the IRS becomes the OIS;
``tests/test_pricing.py`` asserts exactly that, because it is the cheapest
proof that the two-curve plumbing is wired the right way round.

**Terms, not valuation.** A swap here is what was traded: dates, rates, a
side, and the :class:`~rates_engine.conventions.indices.RateIndex` it floats
on. The index fixes the swap's currency and, unless one is given, the
calendar its dates roll on. Projecting the floating leg off a curve is
:mod:`rates_engine.pricing.projection`'s job; the fixed leg needs no curve and
is produced here.

**Sign convention.** A payer pays fixed and receives floating, so its fixed
cashflows are negative and its floating cashflows positive. A receiver is the
exact negation. Every DV01 and duration in this package inherits that.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from functools import cached_property

from rates_engine.conventions.calendar import BusinessDayConvention, HolidayCalendar
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.indices import SOFR, IndexTenor, RateIndex, term_sofr
from rates_engine.conventions.schedule import Schedule
from rates_engine.conventions.side import Side, fixed_leg_sign
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.money import Currency
from rates_engine.instruments.cashflow import Cashflow

__all__ = ["Side", "OISSwap", "IRSwap"]


def _fixed_leg(
    schedule: Schedule,
    *,
    side: str,
    notional: float,
    fixed_rate: float,
    day_count: DayCount,
    currency: Currency,
) -> tuple[Cashflow, ...]:
    """The fixed leg on a schedule, signed for ``side``. One implementation for both swaps."""
    sign = fixed_leg_sign(side)
    return tuple(
        Cashflow(
            payment_date=pay,
            amount=sign * notional * fixed_rate * year_fraction(start, end, day_count),
            leg="fixed",
            accrual_start=start,
            accrual_end=end,
            year_fraction=year_fraction(start, end, day_count),
            rate=fixed_rate,
            currency=currency,
        )
        for start, end, pay in zip(
            schedule.accrual_start, schedule.accrual_end, schedule.payment, strict=True
        )
    )


@dataclass(frozen=True)
class OISSwap:
    """An overnight index swap: fixed against compounded overnight.

    Attributes:
        effective: Start of the first accrual period.
        maturity: End of the last one.
        fixed_rate: Fixed rate as a decimal.
        notional: Notional in the index's currency.
        side: ``"payer"`` or ``"receiver"``.
        frequency_months: Fixed leg frequency; 12 is the market convention
            for a SOFR OIS.
        fixed_day_count: Fixed leg accrual basis.
        payment_lag_days: Business days between accrual end and payment. Two
            is the convention, because the final overnight fixing of a period
            is not published until the period has ended.
        calendar: Calendar for payment rolls. ``None`` rolls on the index's
            own calendar, which is what a peso swap needs and what used to be
            forgotten: before v0.4 every swap rolled on SIFMA unless told
            otherwise.
        convention: Roll rule for payment dates.
        index: The overnight rate the floating leg compounds. It fixes the
            swap's currency; valuing the swap on a curve in another currency
            is refused. SOFR by default.

    Raises:
        UnsupportedConventionError: ``index`` is not an overnight rate.
    """

    effective: date
    maturity: date
    fixed_rate: float
    notional: float = 1_000_000.0
    side: str = Side.PAYER
    frequency_months: int = 12
    fixed_day_count: DayCount = DayCount.ACT_360
    payment_lag_days: int = 2
    calendar: HolidayCalendar | None = None
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    index: RateIndex = SOFR

    def __post_init__(self) -> None:
        if self.index.tenor is not IndexTenor.OVERNIGHT:
            raise UnsupportedConventionError(
                f"an OIS floats on an overnight rate; {self.index.name} is a "
                f"{self.index.tenor.value} term rate. Use IRSwap for a term index."
            )

    @property
    def rate_index(self) -> RateIndex:
        """The index the floating leg pays; every instrument answers this alike."""
        return self.index

    @property
    def currency(self) -> Currency:
        """The currency of every flow, from :attr:`index`."""
        return self.index.currency

    @property
    def roll_calendar(self) -> HolidayCalendar:
        """The calendar dates roll on: :attr:`calendar`, else the index's."""
        return self.calendar if self.calendar is not None else self.index.calendar

    @property
    def span(self) -> tuple[date, date]:
        """First accrual start and last accrual end, as a half-open interval.

        Every instrument answers this the same way, so code that needs to know
        what stretch of the curve a trade touches — the hedge coverage check,
        for one — does not have to know which attribute each product spells
        its dates with.
        """
        return self.effective, self.maturity

    def with_terms(self, **changes: object) -> OISSwap:
        """A copy with some terms changed; the original is untouched.

        Exists so that :mod:`rates_engine.pricing.linear` can build the unit-rate and
        single-side variants a par rate needs without reaching for
        ``dataclasses.replace`` on a protocol, which is not something a type
        checker can verify.

        Args:
            **changes: Field names and their new values.

        Returns:
            The modified copy.
        """
        return replace(self, **changes)  # type: ignore[arg-type]

    @cached_property
    def schedule(self) -> Schedule:
        """Accrual and payment dates for both legs."""
        return Schedule.generate(
            self.effective,
            self.maturity,
            frequency_months=self.frequency_months,
            calendar=self.roll_calendar,
            convention=self.convention,
            payment_lag_days=self.payment_lag_days,
        )

    def fixed_cashflows(self) -> tuple[Cashflow, ...]:
        """The fixed leg, signed for :attr:`side`, in :attr:`currency`.

        Returns:
            One flow per schedule period.
        """
        return _fixed_leg(
            self.schedule,
            side=self.side,
            notional=self.notional,
            fixed_rate=self.fixed_rate,
            day_count=self.fixed_day_count,
            currency=self.currency,
        )

    def describe(self) -> dict[str, object]:
        """Terms of the swap, for the evidence record."""
        return {
            "kind": "ois_swap",
            "effective": self.effective.isoformat(),
            "maturity": self.maturity.isoformat(),
            "fixed_rate": self.fixed_rate,
            "notional": self.notional,
            "side": self.side,
            "frequency_months": self.frequency_months,
            "fixed_day_count": self.fixed_day_count.value,
            "payment_lag_days": self.payment_lag_days,
            "float_index": f"compounded_{self.index.slug}",
        }


@dataclass(frozen=True)
class IRSwap:
    """Fixed against a term rate: projected on the tenor curve, discounted on OIS.

    Attributes:
        effective: Start of the first accrual period.
        maturity: End of the last one.
        fixed_rate: Fixed rate as a decimal.
        notional: Notional in the index's currency.
        side: ``"payer"`` or ``"receiver"``.
        fixed_frequency_months: Fixed leg frequency.
        float_frequency_months: Floating leg frequency, which is also the
            index tenor. ``None`` takes it from :attr:`index`; with no index
            either, three months.
        fixed_day_count: Fixed leg accrual basis.
        float_day_count: Floating leg accrual basis.
        payment_lag_days: Business days from accrual end to payment. Zero by
            default: a term rate is known at the period start, so there is
            nothing to wait for.
        calendar: Calendar for payment rolls; ``None`` rolls on the index's.
        convention: Roll rule for payment dates.
        index: The term rate the floating leg pays. ``None`` means Term SOFR
            of :attr:`float_frequency_months`.

    Raises:
        UnsupportedConventionError: The index is not a term rate in whole
            months (TIIE 28 accrues 28-day periods, which this schedule cannot
            generate), or its tenor disagrees with ``float_frequency_months``.
    """

    effective: date
    maturity: date
    fixed_rate: float
    notional: float = 1_000_000.0
    side: str = Side.PAYER
    fixed_frequency_months: int = 12
    float_frequency_months: int | None = None
    fixed_day_count: DayCount = DayCount.ACT_360
    float_day_count: DayCount = DayCount.ACT_360
    payment_lag_days: int = 0
    calendar: HolidayCalendar | None = None
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    index: RateIndex | None = None

    def __post_init__(self) -> None:
        index = self.float_index
        if index.tenor.months is None:
            raise UnsupportedConventionError(
                f"an IRSwap schedules its floating leg in whole months; {index.name} "
                f"accrues {index.tenor.value} periods and has no instrument here yet"
            )
        if (
            self.float_frequency_months is not None
            and self.float_frequency_months != index.tenor.months
        ):
            raise UnsupportedConventionError(
                f"float_frequency_months={self.float_frequency_months} disagrees with "
                f"{index.name}, a {index.tenor.months}-month rate. Set both together, "
                "or leave float_frequency_months unset to take it from the index."
            )
        # Resolved into both fields, so that the three spellings of one
        # trade -- neither given, the frequency, or the index -- are equal and
        # hash alike. Changing one later with ``replace`` must change both.
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "float_frequency_months", index.tenor.months)

    @property
    def float_index(self) -> RateIndex:
        """The index the floating leg pays, resolved from the two fields."""
        if self.index is not None:
            return self.index
        return term_sofr(self.float_frequency_months or 3)

    @property
    def float_months(self) -> int:
        """Floating leg period in months: the index tenor."""
        months = self.float_index.tenor.months
        assert months is not None  # enforced in __post_init__
        return months

    @property
    def rate_index(self) -> RateIndex:
        """The index the floating leg pays; every instrument answers this alike."""
        return self.float_index

    @property
    def currency(self) -> Currency:
        """The currency of every flow, from the floating index."""
        return self.float_index.currency

    @property
    def roll_calendar(self) -> HolidayCalendar:
        """The calendar dates roll on: :attr:`calendar`, else the index's."""
        return self.calendar if self.calendar is not None else self.float_index.calendar

    @property
    def span(self) -> tuple[date, date]:
        """First accrual start and last accrual end, as a half-open interval."""
        return self.effective, self.maturity

    def with_terms(self, **changes: object) -> IRSwap:
        """A copy with some terms changed; see :meth:`OISSwap.with_terms`.

        Args:
            **changes: Field names and their new values.

        Returns:
            The modified copy.
        """
        return replace(self, **changes)  # type: ignore[arg-type]

    def _schedule(self, frequency_months: int) -> Schedule:
        return Schedule.generate(
            self.effective,
            self.maturity,
            frequency_months=frequency_months,
            calendar=self.roll_calendar,
            convention=self.convention,
            payment_lag_days=self.payment_lag_days,
        )

    @cached_property
    def fixed_schedule(self) -> Schedule:
        """Fixed leg schedule."""
        return self._schedule(self.fixed_frequency_months)

    @cached_property
    def float_schedule(self) -> Schedule:
        """Floating leg schedule."""
        return self._schedule(self.float_months)

    def fixed_cashflows(self) -> tuple[Cashflow, ...]:
        """The fixed leg, signed for :attr:`side`, in :attr:`currency`.

        Returns:
            One flow per fixed-leg period.
        """
        return _fixed_leg(
            self.fixed_schedule,
            side=self.side,
            notional=self.notional,
            fixed_rate=self.fixed_rate,
            day_count=self.fixed_day_count,
            currency=self.currency,
        )

    def describe(self) -> dict[str, object]:
        """Terms of the swap, for the evidence record."""
        return {
            "kind": "ir_swap",
            "effective": self.effective.isoformat(),
            "maturity": self.maturity.isoformat(),
            "fixed_rate": self.fixed_rate,
            "notional": self.notional,
            "side": self.side,
            "fixed_frequency_months": self.fixed_frequency_months,
            "float_frequency_months": self.float_months,
            "fixed_day_count": self.fixed_day_count.value,
            "float_day_count": self.float_day_count.value,
            "float_index": self.float_index.slug,
        }
