"""Market data with its provenance attached, and the SOFR compounding rules.

A ``MarketSnapshot`` is a bag of named series plus the record of where each one
came from. The compounding helpers live here rather than on the instruments
because repeating the last published rate over a non-publication day is a data
convention, not a product feature: SR1, SR3 and an OIS floating leg all inherit
it, and they should inherit one implementation.

The repeated rate is weighted, not repeated: a Friday's SOFR accrues over the
weekend as one simple-interest period of three days, which is how CME settles
SR3 and how the New York Fed compounds its averages. For SR1's arithmetic
average the two readings coincide; for anything compounded they do not.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, timedelta

from rates_engine.conventions.calendar import SIFMA_US, HolidayCalendar
from rates_engine.core.errors import MissingFixingError
from rates_engine.core.evidence import DataQuality, Provenance

__all__ = ["Series", "MarketSnapshot", "CompoundedRate"]


@dataclass(frozen=True)
class Series:
    """One named time series and the record of where it came from.

    Attributes:
        series_id: Identifier, e.g. ``"SOFR"`` or ``"DGS2"``.
        dates: Observation dates, strictly increasing.
        values: Observations, in the series' own units. Overnight rates are
            decimals, so 5.31% is ``0.0531``.
        provenance: Source, retrieval time and data quality.
    """

    series_id: str
    dates: tuple[date, ...]
    values: tuple[float, ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.values):
            raise ValueError(
                f"series {self.series_id!r} has {len(self.dates)} dates "
                f"and {len(self.values)} values"
            )
        if any(b <= a for a, b in zip(self.dates, self.dates[1:], strict=False)):
            raise ValueError(f"series {self.series_id!r} dates must be strictly increasing")

    def __len__(self) -> int:
        """Number of observations."""
        return len(self.dates)

    def get(self, day: date) -> float | None:
        """The observation on ``day``, or ``None`` when there is none."""
        index = bisect.bisect_left(self.dates, day)
        if index < len(self.dates) and self.dates[index] == day:
            return self.values[index]
        return None

    def last_on_or_before(self, day: date) -> tuple[date, float] | None:
        """The most recent observation at or before ``day``, or ``None``."""
        index = bisect.bisect_right(self.dates, day) - 1
        if index < 0:
            return None
        return self.dates[index], self.values[index]

    def window(self, start: date, end: date) -> Series:
        """A copy restricted to ``[start, end]``, keeping the same provenance."""
        kept = [(d, v) for d, v in zip(self.dates, self.values, strict=True) if start <= d <= end]
        return Series(
            self.series_id,
            tuple(d for d, _ in kept),
            tuple(v for _, v in kept),
            self.provenance,
        )


@dataclass(frozen=True)
class CompoundedRate:
    """The result of compounding or averaging an overnight series over a period.

    Attributes:
        rate: The period rate as a decimal, on the ACT/360 basis.
        accrual_factor: The compounded growth factor, ``1`` plus the simple
            equivalent times the year fraction. Exactly ``1.0`` for a period
            of zero length.
        year_fraction: Period length in years, ACT/360.
        business_days: Number of published fixings used.
        repeated_days: Calendar days that reused the previous published rate
            because no rate was published for them. AC-2.4 requires this be
            reported rather than absorbed.
        first_fixing_date: Date of the first fixing used, ``None`` if none were.
        last_fixing_date: Date of the last fixing used, ``None`` if none were.
    """

    rate: float
    accrual_factor: float
    year_fraction: float
    business_days: int
    repeated_days: int
    first_fixing_date: date | None
    last_fixing_date: date | None

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-ready mapping, ``None`` never a missing key."""
        return {
            "rate": self.rate,
            "accrual_factor": self.accrual_factor,
            "year_fraction": self.year_fraction,
            "business_days": self.business_days,
            "repeated_days": self.repeated_days,
            "first_fixing_date": (
                self.first_fixing_date.isoformat() if self.first_fixing_date else None
            ),
            "last_fixing_date": (
                self.last_fixing_date.isoformat() if self.last_fixing_date else None
            ),
        }


@dataclass(frozen=True)
class MarketSnapshot:
    """Named series as of one date, each carrying its own provenance.

    Attributes:
        as_of: The valuation date the snapshot describes.
        series: Series by identifier.
    """

    as_of: date
    series: dict[str, Series]

    @property
    def provenance(self) -> dict[str, Provenance]:
        """Provenance of every series, keyed the same way as :attr:`series`."""
        return {name: s.provenance for name, s in self.series.items()}

    def require(self, series_id: str) -> Series:
        """The named series, or a refusal naming what is missing.

        Args:
            series_id: Identifier to look up.

        Returns:
            The series.

        Raises:
            MissingFixingError: No such series in this snapshot.
        """
        try:
            return self.series[series_id]
        except KeyError:
            raise MissingFixingError(
                f"snapshot as of {self.as_of} has no series {series_id!r}; "
                f"it has {sorted(self.series)}"
            ) from None

    def fixing(self, series_id: str, day: date) -> float:
        """One published fixing, refusing rather than interpolating.

        Args:
            series_id: Series to read.
            day: Business day the fixing is wanted for.

        Returns:
            The published value as a decimal.

        Raises:
            MissingFixingError: The series has no observation on ``day``. The
                message names the date, because an interpolated overnight rate
                silently contaminates every compounded rate spanning it.
        """
        value = self.require(series_id).get(day)
        if value is None:
            raise MissingFixingError(
                f"series {series_id!r} has no fixing for {day}; fixings are never interpolated"
            )
        return value

    def compounded(
        self,
        series_id: str,
        start: date,
        end: date,
        *,
        calendar: HolidayCalendar = SIFMA_US,
    ) -> CompoundedRate:
        """Compound an overnight series over ``[start, end)``, ACT/360, the way SR3 settles.

        The factor is ``prod_i (1 + r_i * d_i / 360)``, the product running
        over the publication days ``i`` in the period, ``r_i`` the rate
        published for day ``i`` and ``d_i`` the calendar days it applies to.
        That is CME's SR3 final-settlement formula and the New York Fed's for
        its SOFR averages, and it is what QuantLib's
        ``OvernightIndexedCoupon`` computes. A Friday's rate covers Friday,
        Saturday and Sunday as *one* simple-interest period, ``1 + 3r/360``.
        Compounding the three days separately, ``(1 + r/360)**3``, is what
        this method did before, and at 4-5% it put SR3 0.02-0.04 bp too high,
        USD 0.6-1.1 a contract.

        Adjacent periods multiply to the whole only when the boundary between
        them is a publication day, so that every published rate and the days
        it covers sit in one period. When the boundary is a weekend, each side
        accrues its own share of the carried rate's days, which is the
        convention -- a period's factor is a function of that period -- and
        the product of the two exceeds the whole by a second-order term, about
        ``2.5e-8`` at 4% across a weekend. None of this touches the
        bootstrap's exactness in :mod:`rates_engine.curves.bootstrap`: a
        ``RealizedStubNode`` takes the period factor as its
        ``accrual_factor`` and a ``FuturesNode`` takes ``1 + F * tau``, each
        pins ``P(start) / P(end)`` to that number, and the curve reprices it
        whichever daily rule produced it.

        Args:
            series_id: Overnight series to compound, e.g. ``"SOFR"``.
            start: First accrual day, inclusive.
            end: Last accrual day, exclusive.
            calendar: Publication calendar. A non-business day takes the rate
                published for the business day before it, per the CME and New
                York Fed rule, and accrues at it as part of that day's period.

        Returns:
            The :class:`CompoundedRate`, with the count of repeated days.

        Raises:
            MissingFixingError: A business day in the period has no fixing.
            ValueError: ``end`` precedes ``start``.
        """
        factor, business_days, repeated, first, last = self._accrue(
            series_id, start, end, calendar
        )
        tau = (end - start).days / 360.0
        rate = (factor - 1.0) / tau if tau else 0.0
        return CompoundedRate(rate, factor, tau, business_days, repeated, first, last)

    def averaged(
        self,
        series_id: str,
        start: date,
        end: date,
        *,
        calendar: HolidayCalendar = SIFMA_US,
    ) -> CompoundedRate:
        """Arithmetic-average an overnight series over ``[start, end)``, ACT/360 weighted.

        This is SR1's settlement basis: the simple average of the daily rates
        weighted by calendar days, not the compounded rate SR3 uses. The two
        differ by a fraction of a basis point at current levels, which is more
        than the 0.1 bp tolerance the settlement tests hold them to.

        Args:
            series_id: Overnight series to average.
            start: First accrual day, inclusive.
            end: Last accrual day, exclusive.
            calendar: Publication calendar; non-business days reuse the
                previous published rate.

        Returns:
            The :class:`CompoundedRate`, whose ``accrual_factor`` is the simple
            ``1 + rate * year_fraction``.

        Raises:
            MissingFixingError: A business day in the period has no fixing.
            ValueError: ``end`` precedes ``start``.
        """
        total, business_days, repeated, first, last = self._sum(series_id, start, end, calendar)
        days = (end - start).days
        rate = total / days if days else 0.0
        tau = days / 360.0
        return CompoundedRate(rate, 1.0 + rate * tau, tau, business_days, repeated, first, last)

    def _daily(
        self, series_id: str, start: date, end: date, calendar: HolidayCalendar
    ) -> list[tuple[date, float, bool]]:
        """Per-calendar-day rates over ``[start, end)``, flagged when repeated."""
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")
        series = self.require(series_id)
        out: list[tuple[date, float, bool]] = []
        day = start
        last_published: float | None = None
        while day < end:
            if calendar.is_business_day(day):
                value = series.get(day)
                if value is None:
                    raise MissingFixingError(
                        f"series {series_id!r} has no fixing for business day {day}; "
                        "fixings are never interpolated"
                    )
                last_published = value
                out.append((day, value, False))
            else:
                if last_published is None:
                    previous = series.last_on_or_before(day)
                    if previous is None:
                        raise MissingFixingError(
                            f"series {series_id!r} has no fixing on or before {day} to carry "
                            "into the non-publication day that starts this period"
                        )
                    last_published = previous[1]
                out.append((day, last_published, True))
            day += timedelta(days=1)
        return out

    def _accrue(
        self, series_id: str, start: date, end: date, calendar: HolidayCalendar
    ) -> tuple[float, int, int, date | None, date | None]:
        """Compounded factor and the bookkeeping the evidence needs.

        One simple-interest factor per run of calendar days that share a
        published rate, not one per calendar day: each publication day opens
        a run, the non-publication days after it extend it, and a period
        that opens on a non-publication day starts with a run at the rate it
        carries in. Runs are cut by publication days, never by equal rates:
        two business days that happen to publish the same number are two
        factors.
        """
        daily = self._daily(series_id, start, end, calendar)
        run_rates: list[float] = []
        run_days: list[int] = []
        for _, rate, repeated in daily:
            if repeated and run_rates:
                run_days[-1] += 1
            else:
                run_rates.append(rate)
                run_days.append(1)
        factor = 1.0
        for rate, days in zip(run_rates, run_days, strict=True):
            factor *= 1.0 + rate * days / 360.0
        published = [d for d, _, repeated in daily if not repeated]
        return (
            factor,
            len(published),
            sum(1 for _, _, repeated in daily if repeated),
            published[0] if published else None,
            published[-1] if published else None,
        )

    def _sum(
        self, series_id: str, start: date, end: date, calendar: HolidayCalendar
    ) -> tuple[float, int, int, date | None, date | None]:
        """Day-weighted sum of rates and the same bookkeeping."""
        daily = self._daily(series_id, start, end, calendar)
        total = sum(rate for _, rate, _ in daily)
        published = [d for d, _, repeated in daily if not repeated]
        return (
            total,
            len(published),
            sum(1 for _, _, repeated in daily if repeated),
            published[0] if published else None,
            published[-1] if published else None,
        )

    def worst_quality(self) -> DataQuality:
        """The least trustworthy data quality among the series held."""
        if not self.series:
            return DataQuality.OBSERVED
        return max((s.provenance.data_quality for s in self.series.values()), key=lambda q: q.rank)
