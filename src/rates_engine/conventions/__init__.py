"""Day counts, calendars, rolls and schedules — every one of them explicit.

Nothing in this package accepts a convention as a bare string on a hot path.
The enums exist so that an unsupported convention is a refusal at construction
time rather than a plausible number later.
"""

from rates_engine.conventions.calendar import (
    SIFMA_US,
    BusinessDayConvention,
    Calendar,
    SIFMAUSCalendar,
    easter_sunday,
)
from rates_engine.conventions.daycount import DayCount, day_count_from_name, year_fraction
from rates_engine.conventions.schedule import (
    Schedule,
    add_months,
    imm_date,
    imm_dates,
    next_imm_on_or_after,
)

__all__ = [
    "BusinessDayConvention",
    "Calendar",
    "DayCount",
    "SIFMAUSCalendar",
    "SIFMA_US",
    "Schedule",
    "add_months",
    "day_count_from_name",
    "easter_sunday",
    "imm_date",
    "imm_dates",
    "next_imm_on_or_after",
    "year_fraction",
]
