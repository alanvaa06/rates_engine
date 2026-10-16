"""Market data with its provenance attached, and the SOFR compounding rules.

A ``MarketSnapshot`` is a bag of named series plus the record of where each one
came from. The compounding helpers live here rather than on the instruments
because repeating the last published rate over a non-publication day is a data
convention, not a product feature: SR1, SR3 and an OIS floating leg all inherit
it, and they should inherit one implementation.

Repeating a rate does not mean compounding it again: a Friday's SOFR accrues
over the weekend as one simple-interest period of three days, which is how CME
settles SR3 and how the New York Fed compounds its averages. For SR1's
arithmetic average the two readings coincide; for anything compounded they do
not.
"""

from __future__ import annotations

import bisect
import math
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
            decimals, so 5.31% is ``0.0531``. Finite: a day without an
            observation is absent from ``dates``, not present as NaN.
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
        # A NaN is not None, so every "is there a fixing?" check downstream
        # counted it as present, and it came out of compounding as a NaN
        # settlement price with no error. A missing observation is left out
        # of the series, which is what MissingFixingError then catches.
        for day, value in zip(self.dates, self.values, strict=True):
            if not math.isfinite(value):
                raise ValueError(
                    f"series {self.series_id!r} has {value!r} on {day}. A missing "
                    "observation is left out of the series, never stored as NaN or "
                    "infinity; drop the row, or supply the value"
                )

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

    **Nothing dated after** ``as_of`` **is served.** :meth:`fixing`,
    :meth:`compounded` and :meth:`averaged` refuse a date past it with
    :class:`~rates_engine.core.errors.MissingFixingError`, and
    :meth:`require` returns the series cut at it. ``as_of`` used to be a
    label: a snapshot as of 15 January served 10 February's fixing and
    compounded through 30 January, which in a backtest is the future
    leaking into the past with nothing in the result to say so. The
    fixing dated ``as_of`` itself is served, the convention
    :func:`~rates_engine.market.estimators.realized_sofr_sigma` already
    used. :attr:`series` keeps what was loaded, so one history can be
    loaded once and wrapped in a snapshot per backtest date.

    Attributes:
        as_of: The valuation date the snapshot describes.
        series: Series by identifier, as loaded, including any observations
            dated after ``as_of``; read them through :meth:`require`.
    """

    as_of: date
    series: dict[str, Series]

    @property
    def provenance(self) -> dict[str, Provenance]:
        """Provenance of every series, keyed the same way as :attr:`series`."""
        return {name: s.provenance for name, s in self.series.items()}

    def require(self, series_id: str) -> Series:
        """The named series as known on ``as_of``, or a refusal naming what is missing.

        Args:
            series_id: Identifier to look up.

        Returns:
            The series without its observations dated after :attr:`as_of`,
            with the same provenance. The same object when there are none.

        Raises:
            MissingFixingError: No such series in this snapshot.
        """
        try:
            series = self.series[series_id]
        except KeyError:
            raise MissingFixingError(
                f"snapshot as of {self.as_of} has no series {series_id!r}; "
                f"it has {sorted(self.series)}"
            ) from None
        if series.dates and series.dates[-1] > self.as_of:
            return series.window(series.dates[0], self.as_of)
        return series

    def _refuse_after_as_of(self, series_id: str, day: date, needed_by: str, remedy: str) -> None:
        """Raise when ``day`` is after :attr:`as_of`, naming what needed it and the way out."""
        if day > self.as_of:
            raise MissingFixingError(
                f"{needed_by} needs the {series_id!r} fixing for {day}, which is after this "
                f"snapshot's valuation date {self.as_of}. A snapshot serves nothing dated "
                f"after its as_of, so that a backtest cannot see the future; {remedy}"
            )

    def fixing(self, series_id: str, day: date) -> float:
        """One published fixing, refusing rather than interpolating.

        Args:
            series_id: Series to read.
            day: Business day the fixing is wanted for, on or before
                :attr:`as_of`.

        Returns:
            The published value as a decimal.

        Raises:
            MissingFixingError: The series has no observation on ``day``, or
                ``day`` is after :attr:`as_of`. The message names the date,
                because an interpolated overnight rate silently contaminates
                every compounded rate spanning it.
        """
        self._refuse_after_as_of(
            series_id, day, "this lookup", "build the snapshot as of a later date to read it"
        )
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
        this method did before, and on flat SOFR of 4.30-5.33% it put SR3
        0.025-0.045 bp too high, USD 0.6-1.1 a contract.

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
            MissingFixingError: A business day in the period has no fixing,
                or the period opens on a non-business day and the business
                day before it has none (an older fixing is never carried
                in), or a business day in the period is after :attr:`as_of`.
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
            MissingFixingError: A business day in the period has no fixing,
                or the period opens on a non-business day and the business
                day before it has none (an older fixing is never carried
                in), or a business day in the period is after :attr:`as_of`.
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
        """Per-calendar-day rates over ``[start, end)``, flagged when repeated.

        A business day after :attr:`as_of` is refused before its fixing is
        looked up. A non-business day after it is not: what it accrues is
        fixed by the rate of the business day before it, which the snapshot
        holds, so a snapshot as of a Friday serves the weekend after it.
        """
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")
        series = self.require(series_id)
        out: list[tuple[date, float, bool]] = []
        day = start
        last_published: float | None = None
        while day < end:
            if calendar.is_business_day(day):
                self._refuse_after_as_of(
                    series_id,
                    day,
                    f"the period [{start}, {end})",
                    f"accrue the realised part only up to {self.as_of + timedelta(days=1)} "
                    "and project the rest from a curve, or build the snapshot as of a later "
                    "date",
                )
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
                    last_published = self._carried_in(series, series_id, day, calendar)
                out.append((day, last_published, True))
            day += timedelta(days=1)
        return out

    def _carried_in(
        self, series: Series, series_id: str, day: date, calendar: HolidayCalendar
    ) -> float:
        """The rate a period opening on non-publication ``day`` carries in.

        It is the fixing of the business day immediately before ``day`` on
        ``calendar``, and only that one. Reaching for the last observation on
        file instead was the old behaviour, and it had no bound: with July
        missing, an August SR1 carried 30 June's rate into 1-2 August and
        settled 1.46 bp away without a word. A gap in the data is a refusal
        here exactly as it is on a business day inside the period.
        """
        previous = calendar.previous_business_day(day)
        self._refuse_after_as_of(
            series_id,
            previous,
            f"the period opening on {day}",
            "build the snapshot as of a later date",
        )
        value = series.get(previous)
        if value is None:
            held = series.last_on_or_before(previous)
            found = (
                f"the last fixing on file before it is {held[0]}, which is not the "
                "rate this day takes"
                if held
                else "the series holds nothing that early"
            )
            raise MissingFixingError(
                f"series {series_id!r} has no fixing for {previous}, the business day "
                f"before {day} that opens this period and whose rate {day} takes; {found}. "
                "Supply that fixing: an older one is never carried in its place"
            )
        return value

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
