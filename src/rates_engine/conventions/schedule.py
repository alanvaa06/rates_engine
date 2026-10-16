"""IMM dates and payment schedules.

Two things live here because they are the same question asked twice: which
dates does this contract touch. ``imm_dates`` answers it for the futures strip,
``Schedule`` for a swap's legs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from rates_engine.conventions.calendar import (
    SIFMA_US,
    BusinessDayConvention,
    HolidayCalendar,
    _nth_weekday,
)

__all__ = ["imm_dates", "imm_date", "next_imm_on_or_after", "add_months", "Schedule"]

_IMM_MONTHS = (3, 6, 9, 12)


def imm_date(year: int, month: int) -> date:
    """The IMM date for one quarterly month: the third Wednesday.

    Args:
        year: Calendar year.
        month: One of 3, 6, 9, 12.

    Returns:
        The third Wednesday of that month, unadjusted. SR3 contracts start and
        end on these dates whether or not they are business days, which is why
        no roll is applied here.

    Raises:
        ValueError: ``month`` is not an IMM month.
    """
    if month not in _IMM_MONTHS:
        raise ValueError(f"{month} is not an IMM month; expected one of {_IMM_MONTHS}")
    return _nth_weekday(year, month, 2, 3)


def imm_dates(year: int) -> tuple[date, date, date, date]:
    """The four IMM dates of a year: third Wednesdays of March, June, September, December.

    Args:
        year: Calendar year.

    Returns:
        The four dates in calendar order.
    """
    return tuple(imm_date(year, m) for m in _IMM_MONTHS)  # type: ignore[return-value]


def next_imm_on_or_after(day: date) -> date:
    """The first IMM date on or after ``day``.

    Args:
        day: Any date.

    Returns:
        The next third Wednesday of an IMM month, ``day`` itself if it is one.
    """
    for year in (day.year, day.year + 1):
        for candidate in imm_dates(year):
            if candidate >= day:
                return candidate
    raise AssertionError("unreachable: an IMM date always exists within a year")  # pragma: no cover


def add_months(day: date, months: int) -> date:
    """Shift by whole months, clamping to the last valid day of the target month.

    Public because two callers need it and two implementations of month
    arithmetic is two chances to disagree about what a month after 31 January
    is. Negative counts go backwards.

    Args:
        day: Starting date.
        months: Whole months to shift by; negative goes back.

    Returns:
        The shifted date, clamped to the target month's last day.
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    following = date(year + month // 12, month % 12 + 1, 1)
    last_day = (following - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


@dataclass(frozen=True)
class Schedule:
    """Accrual periods and payment dates for one leg.

    Attributes:
        accrual_start: Start of each period: the effective date, adjusted by
            the roll convention unless the schedule was generated with
            ``adjust_accrual_ends=False``, then each previous period's end.
        accrual_end: End of each period, adjusted by the roll convention
            unless the schedule was generated with
            ``adjust_accrual_ends=False``.
        payment: Payment date of each period: the payment lag in business
            days after the (adjusted) accrual end.
    """

    accrual_start: tuple[date, ...]
    accrual_end: tuple[date, ...]
    payment: tuple[date, ...]

    def __len__(self) -> int:
        """Number of accrual periods."""
        return len(self.accrual_start)

    @classmethod
    def generate(
        cls,
        effective: date,
        maturity: date,
        *,
        frequency_months: int,
        calendar: HolidayCalendar = SIFMA_US,
        convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
        payment_lag_days: int = 0,
        adjust_accrual_ends: bool = True,
    ) -> Schedule:
        """Build a schedule backwards from maturity, the market convention.

        Generating backwards means any short period lands at the front, where
        a stub belongs, rather than at the back where it would shorten the
        final coupon nobody expects to be short.

        **Adjusted period ends.** Each period ends on its roll date adjusted
        by ``convention``, and is paid ``payment_lag_days`` business days
        later. That is the USD SOFR OIS convention (period ends Modified
        Following, payment two business days after -- CME's Eris SOFR
        contract terms) and the 2006 ISDA Definitions default, under which
        period end dates adjust with the payment dates. Until v0.4 the ends
        were left unadjusted while the payments rolled, so an accrual period
        and its payment disagreed whenever a roll date fell on a holiday or a
        weekend: interest accrued to a Saturday was paid on the Monday. The
        roll dates themselves are still counted from the unadjusted maturity,
        so an adjustment never drifts into the next period.

        **The effective date is adjusted too.** The first period starts on
        ``effective`` rolled by ``convention``, as in QuantLib's ``Schedule``,
        which adjusts its first date with the same convention as the rolls.
        Until v0.4 it was used as given, so a swap struck to start on the
        June 2030 IMM date -- Juneteenth, a SIFMA holiday -- accrued from the
        holiday rather than from the Thursday after. This moves a number only
        when ``effective`` is not a business day; on a business day the
        adjustment returns it unchanged.

        **A first period that rolls onto the start is dropped.** When the
        convention pulls the first roll date back onto the adjusted
        effective date -- an effective date of Friday 29 May 2026 and a roll
        on Sunday 31 May, which Modified Following returns to the Friday --
        the period it would close has no days in it. Until v0.4 the schedule
        kept it: an OIS carried an empty flow and an ``IRSwap`` could not be
        priced at all. The roll date is removed, so the first period runs to
        the next one, which is what QuantLib's ``Schedule`` does.

        Args:
            effective: Start of the first accrual period, before adjustment.
            maturity: End of the last accrual period, before adjustment.
            frequency_months: Months per period; 12 for annual, 3 for quarterly.
            calendar: Calendar the payment roll uses.
            convention: Roll rule for payment dates.
            payment_lag_days: Business days between accrual end and payment.
            adjust_accrual_ends: ``False`` reproduces the pre-v0.4 schedule,
                with an unadjusted effective date and period ends and only the
                payments rolled.

        Returns:
            The schedule.

        Raises:
            ValueError: ``maturity`` is not after ``effective``, the
                frequency is not positive, or ``maturity`` adjusts onto or
                before the adjusted effective date, which leaves no period.
        """
        if maturity <= effective:
            raise ValueError(f"maturity {maturity} must be after effective {effective}")
        if frequency_months <= 0:
            raise ValueError(f"frequency_months must be positive, got {frequency_months}")

        ends: list[date] = []
        cursor = maturity
        while cursor > effective:
            ends.append(cursor)
            cursor = add_months(maturity, -frequency_months * (len(ends)))
        ends.reverse()
        start = effective
        if adjust_accrual_ends:
            start = calendar.adjust(effective, convention)
            ends = [calendar.adjust(end, convention) for end in ends]
            # Roll dates are strictly after the unadjusted effective date, but
            # adjusting both can land the first on the second. Only the front
            # can collapse: later roll dates are a whole period apart, and no
            # adjustment moves a date by that much.
            while len(ends) > 1 and ends[0] <= start:
                ends.pop(0)
            if ends[0] <= start:
                raise ValueError(
                    f"maturity {maturity} adjusts to {ends[0]} under {convention.value}, "
                    f"which is not after the adjusted effective date {start}, so the "
                    "schedule has no accrual period; move the maturity to a later date"
                )
        starts = [start, *ends[:-1]]

        payments = tuple(
            calendar.adjust(
                calendar.add_business_days(end, payment_lag_days) if payment_lag_days else end,
                convention,
            )
            for end in ends
        )
        return cls(tuple(starts), tuple(ends), payments)
