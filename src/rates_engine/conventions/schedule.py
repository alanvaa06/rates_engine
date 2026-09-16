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
    SIFMAUSCalendar,
    _nth_weekday,
)

__all__ = ["imm_dates", "imm_date", "next_imm_on_or_after", "Schedule"]

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


def _add_months(day: date, months: int) -> date:
    """Shift by whole months, clamping to the last valid day of the target month."""
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
        accrual_start: Unadjusted start of each period.
        accrual_end: Unadjusted end of each period.
        payment: Adjusted payment date of each period, after the payment lag.
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
        calendar: SIFMAUSCalendar = SIFMA_US,
        convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
        payment_lag_days: int = 0,
    ) -> Schedule:
        """Build a schedule backwards from maturity, the market convention.

        Generating backwards means any short period lands at the front, where
        a stub belongs, rather than at the back where it would shorten the
        final coupon nobody expects to be short.

        Args:
            effective: Start of the first accrual period.
            maturity: End of the last accrual period.
            frequency_months: Months per period; 12 for annual, 3 for quarterly.
            calendar: Calendar the payment roll uses.
            convention: Roll rule for payment dates.
            payment_lag_days: Business days between accrual end and payment.

        Returns:
            The schedule.

        Raises:
            ValueError: ``maturity`` is not after ``effective``, or the
                frequency is not positive.
        """
        if maturity <= effective:
            raise ValueError(f"maturity {maturity} must be after effective {effective}")
        if frequency_months <= 0:
            raise ValueError(f"frequency_months must be positive, got {frequency_months}")

        ends: list[date] = []
        cursor = maturity
        while cursor > effective:
            ends.append(cursor)
            cursor = _add_months(maturity, -frequency_months * (len(ends)))
        ends.reverse()
        starts = [effective, *ends[:-1]]

        payments = tuple(
            calendar.adjust(
                calendar.add_business_days(end, payment_lag_days) if payment_lag_days else end,
                convention,
            )
            for end in ends
        )
        return cls(tuple(starts), tuple(ends), payments)
