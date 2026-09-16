"""Day count conventions, as an enum rather than a string nobody validates.

Three are implemented. A fourth name does not fall back to a default — it
raises :class:`~rates_engine.errors.UnsupportedConventionError`, because a year
fraction computed on the wrong basis is a wrong number that looks entirely
plausible.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from rates_engine.errors import UnsupportedConventionError

__all__ = ["DayCount", "year_fraction", "day_count_from_name"]


class DayCount(Enum):
    """Supported day count bases.

    ``ACT_360``
        Actual days over 360. The SOFR basis: overnight accruals, futures
        settlement averages, floating legs.
    ``ACT_365F``
        Actual days over a fixed 365. Used here as the curve's internal time
        axis, so that interpolation does not inherit a money-market basis.
    ``THIRTY_360``
        US bond basis (30/360, NASD). Fixed legs quoted that way.
    """

    ACT_360 = "ACT/360"
    ACT_365F = "ACT/365F"
    THIRTY_360 = "30/360"


def day_count_from_name(name: str) -> DayCount:
    """Resolve a day count by its market name, refusing anything unimplemented.

    Args:
        name: Market spelling, e.g. ``"ACT/360"``. Case and separators are
            normalised, so ``"act-360"`` resolves too.

    Returns:
        The matching :class:`DayCount`.

    Raises:
        UnsupportedConventionError: The name is not one this package
            implements. The message carries the name as given, never a
            silently substituted default.
    """
    normalised = name.strip().upper().replace("-", "/").replace("_", "/").replace(" ", "")
    for candidate in DayCount:
        if candidate.value.replace("-", "/") == normalised:
            return candidate
    raise UnsupportedConventionError(
        f"day count {name!r} is not implemented; supported: "
        + ", ".join(sorted(c.value for c in DayCount))
    )


def year_fraction(start: date, end: date, day_count: DayCount) -> float:
    """Accrual fraction of a year between two dates, in years.

    Args:
        start: Accrual start, inclusive.
        end: Accrual end, exclusive.
        day_count: Basis to measure on.

    Returns:
        Year fraction in years. Negative when ``end`` precedes ``start``, which
        the caller is expected to mean.

    Raises:
        UnsupportedConventionError: ``day_count`` is not a :class:`DayCount`.
    """
    if not isinstance(day_count, DayCount):
        raise UnsupportedConventionError(
            f"day count {day_count!r} is not a DayCount member; "
            "pass the enum, not a bare string"
        )
    if day_count is DayCount.ACT_360:
        return (end - start).days / 360.0
    if day_count is DayCount.ACT_365F:
        return (end - start).days / 365.0
    return _thirty_360_days(start, end) / 360.0


def _thirty_360_days(start: date, end: date) -> int:
    """Day count under the US 30/360 (NASD) rule, in days.

    The two adjustments are the whole convention: a 31st start becomes the
    30th, and a 31st end becomes the 30th only when the start was already on
    or after the 30th. Applying the second without the first is the classic
    off-by-one that shows up as a fraction of a basis point on long fixed legs.
    """
    d1, d2 = start.day, end.day
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 >= 30:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)
