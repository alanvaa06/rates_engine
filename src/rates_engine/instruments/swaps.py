"""OIS and IRS: a fixed leg, a floating leg, and which curve each one sees.

The difference between the two products is one line of code and the whole
post-LIBOR argument. An OIS floats on compounded overnight, which *is* the
discount curve's own forward, so one curve answers both questions. An IRS
floats on a term rate whose forward lives on a different curve, and only the
discounting stays on OIS. Collapse the two curves and the IRS becomes the OIS;
``tests/test_pricing.py`` asserts exactly that, because it is the cheapest
proof that the two-curve plumbing is wired the right way round.

**Sign convention.** A payer pays fixed and receives floating, so its fixed
cashflows are negative and its floating cashflows positive. A receiver is the
exact negation. Every DV01 and duration in this package inherits that.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

from rates_engine.conventions.calendar import SIFMA_US, BusinessDayConvention, SIFMAUSCalendar
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.schedule import Schedule
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.instruments.side import Side, fixed_leg_sign

if TYPE_CHECKING:  # pragma: no cover - import for typing only, avoids a cycle
    from rates_engine.curves.discount import CurveSet

__all__ = ["Side", "OISSwap", "IRSwap"]


@dataclass(frozen=True)
class OISSwap:
    """A SOFR overnight index swap: fixed against compounded overnight.

    Attributes:
        effective: Start of the first accrual period.
        maturity: End of the last one.
        fixed_rate: Fixed rate as a decimal.
        notional: Notional in USD.
        side: ``"payer"`` or ``"receiver"``.
        frequency_months: Fixed leg frequency; 12 is the market convention
            for a SOFR OIS.
        fixed_day_count: Fixed leg accrual basis.
        payment_lag_days: Business days between accrual end and payment. Two
            is the convention, because the final overnight fixing of a period
            is not published until the period has ended.
        calendar: Calendar for payment rolls.
        convention: Roll rule for payment dates.
    """

    effective: date
    maturity: date
    fixed_rate: float
    notional: float = 1_000_000.0
    side: str = Side.PAYER
    frequency_months: int = 12
    fixed_day_count: DayCount = DayCount.ACT_360
    payment_lag_days: int = 2
    calendar: SIFMAUSCalendar = SIFMA_US
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING

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

        Exists so that :mod:`rates_engine.pricing` can build the unit-rate and
        single-side variants a par rate needs without reaching for
        ``dataclasses.replace`` on a protocol, which is not something a type
        checker can verify.

        Args:
            **changes: Field names and their new values.

        Returns:
            The modified copy.
        """
        return replace(self, **changes)  # type: ignore[arg-type]

    @property
    def schedule(self) -> Schedule:
        """Accrual and payment dates for both legs."""
        return Schedule.generate(
            self.effective,
            self.maturity,
            frequency_months=self.frequency_months,
            calendar=self.calendar,
            convention=self.convention,
            payment_lag_days=self.payment_lag_days,
        )

    def fixed_cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The fixed leg, signed for :attr:`side`.

        ``curve_set`` contributes no rate to a fixed leg, but it does say
        what currency the flows are in: an instrument is denominated by the
        curve it is valued on, not by a field of its own.
        """
        currency = curve_set.currency
        sign = fixed_leg_sign(self.side)
        schedule = self.schedule
        return tuple(
            Cashflow(
                payment_date=pay,
                amount=sign
                * self.notional
                * self.fixed_rate
                * year_fraction(start, end, self.fixed_day_count),
                leg="fixed",
                accrual_start=start,
                accrual_end=end,
                year_fraction=year_fraction(start, end, self.fixed_day_count),
                rate=self.fixed_rate,
                currency=currency,
            )
            for start, end, pay in zip(
                schedule.accrual_start, schedule.accrual_end, schedule.payment
            , strict=True)
        )

    def float_cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The floating leg: compounded overnight, projected off the discount curve.

        The compounded overnight growth over a period is exactly the ratio of
        the period's discount factors on a deterministic curve, so no separate
        projection is needed and none is invented.
        """
        sign = -fixed_leg_sign(self.side)
        curve = curve_set.discount
        schedule = self.schedule
        flows: list[Cashflow] = []
        for start, end, pay in zip(
            schedule.accrual_start, schedule.accrual_end, schedule.payment
        , strict=True):
            tau = year_fraction(start, end, DayCount.ACT_360)
            growth = curve.df(start) / curve.df(end)
            rate = (growth - 1.0) / tau if tau else 0.0
            flows.append(
                Cashflow(
                    payment_date=pay,
                    amount=sign * self.notional * (growth - 1.0),
                    leg="float",
                    accrual_start=start,
                    accrual_end=end,
                    year_fraction=tau,
                    rate=rate,
                    currency=curve_set.currency,
                )
            )
        return tuple(flows)

    def cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """Both legs, fixed first, each signed for :attr:`side`."""
        return self.fixed_cashflows(curve_set) + self.float_cashflows(curve_set)

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
            "float_index": "compounded_sofr",
        }


@dataclass(frozen=True)
class IRSwap:
    """Fixed against Term SOFR: projected on the tenor curve, discounted on OIS.

    Attributes:
        effective: Start of the first accrual period.
        maturity: End of the last one.
        fixed_rate: Fixed rate as a decimal.
        notional: Notional in USD.
        side: ``"payer"`` or ``"receiver"``.
        fixed_frequency_months: Fixed leg frequency.
        float_frequency_months: Floating leg frequency; also the index tenor.
        fixed_day_count: Fixed leg accrual basis.
        float_day_count: Floating leg accrual basis.
        payment_lag_days: Business days from accrual end to payment. Zero by
            default: a term rate is known at the period start, so there is
            nothing to wait for.
        calendar: Calendar for payment rolls.
        convention: Roll rule for payment dates.
    """

    effective: date
    maturity: date
    fixed_rate: float
    notional: float = 1_000_000.0
    side: str = Side.PAYER
    fixed_frequency_months: int = 12
    float_frequency_months: int = 3
    fixed_day_count: DayCount = DayCount.ACT_360
    float_day_count: DayCount = DayCount.ACT_360
    payment_lag_days: int = 0
    calendar: SIFMAUSCalendar = SIFMA_US
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING

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
            calendar=self.calendar,
            convention=self.convention,
            payment_lag_days=self.payment_lag_days,
        )

    @property
    def fixed_schedule(self) -> Schedule:
        """Fixed leg schedule."""
        return self._schedule(self.fixed_frequency_months)

    @property
    def float_schedule(self) -> Schedule:
        """Floating leg schedule."""
        return self._schedule(self.float_frequency_months)

    def fixed_cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The fixed leg, signed for :attr:`side`.

        ``curve_set`` contributes no rate here, but it does say what
        currency the flows are in.
        """
        currency = curve_set.currency
        sign = fixed_leg_sign(self.side)
        schedule = self.fixed_schedule
        return tuple(
            Cashflow(
                payment_date=pay,
                amount=sign
                * self.notional
                * self.fixed_rate
                * year_fraction(start, end, self.fixed_day_count),
                leg="fixed",
                accrual_start=start,
                accrual_end=end,
                year_fraction=year_fraction(start, end, self.fixed_day_count),
                rate=self.fixed_rate,
                currency=currency,
            )
            for start, end, pay in zip(
                schedule.accrual_start, schedule.accrual_end, schedule.payment
            , strict=True)
        )

    def float_cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """The floating leg: forwards from the projection curve, nothing else.

        This is the whole multi-curve story in four lines. The forward comes
        from ``curve_set.projection``; the discounting, applied in
        :mod:`rates_engine.pricing`, comes from ``curve_set.discount``. When
        the two are the same object the basis is zero and the result collapses
        onto the OIS answer.
        """
        sign = -fixed_leg_sign(self.side)
        projection = curve_set.projection
        schedule = self.float_schedule
        flows: list[Cashflow] = []
        for start, end, pay in zip(
            schedule.accrual_start, schedule.accrual_end, schedule.payment
        , strict=True):
            tau = year_fraction(start, end, self.float_day_count)
            rate = projection.forward(start, end, day_count=self.float_day_count)
            flows.append(
                Cashflow(
                    payment_date=pay,
                    amount=sign * self.notional * rate * tau,
                    leg="float",
                    accrual_start=start,
                    accrual_end=end,
                    year_fraction=tau,
                    rate=rate,
                    currency=curve_set.currency,
                )
            )
        return tuple(flows)

    def cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        """Both legs, fixed first, each signed for :attr:`side`."""
        return self.fixed_cashflows(curve_set) + self.float_cashflows(curve_set)

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
            "float_frequency_months": self.float_frequency_months,
            "fixed_day_count": self.fixed_day_count.value,
            "float_day_count": self.float_day_count.value,
            "float_index": f"term_sofr_{self.float_frequency_months}m",
        }
