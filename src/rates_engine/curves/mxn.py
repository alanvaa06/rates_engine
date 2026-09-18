"""The MXN curve, and the conventions this build could not verify.

Everything here works. What it does not do is claim to know the Mexican
conventions it runs on, because PRD-003's research gate could not reach a
primary source for any of them: Banxico, ISDA and CME all return 403 from
the build environment, and QuantLib — the one substantive source that is
reachable — has no TIIE index at all.

That leaves two honest options. Guess the conventions and let every peso
price rest on an unmarked assumption, or build the machinery and carry the
gap in the evidence. AC-1.4 chose the second before the research ran, which
is why this module has the shape it does: every result names the
conventions it assumed in :data:`UNRESOLVED_MXN`, carries a
:class:`~rates_engine.evidence.Degradation` per convention that pushes
``worst_quality`` to ``ASSUMED``, and refuses outright under
``strict_conventions=True``.

**What resolving them looks like.** Someone with a browser and half an hour
reads Banxico's SIE pages for TIIE 28 and TIIE de Fondeo. Then
:data:`UNRESOLVED_MXN` shrinks, the degradations stop being emitted, and no
other code changes. The gap is a data gap, and it is stored as one.

**The two benchmarks never mix unmarked.** TIIE 28 is the historical
interbank rate; TIIE de Fondeo is the overnight funding rate that replaced
it as the reference. A curve built from one is not a curve built from the
other, and :class:`MXNCurveResult` says which, so that comparing two of
them is a deliberate act rather than an accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from rates_engine.conventions.calendar import BMV, BusinessDayConvention
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.bootstrap import (
    DEFAULT_TOLERANCE_BP,
    CalibrationInstrument,
    ParSwapNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import DiscountCurve
from rates_engine.errors import UnresolvedConventionError
from rates_engine.evidence import DataQuality, Degradation, Evidence
from rates_engine.money import Currency
from rates_engine.results import EngineResult

__all__ = [
    "TIIEBenchmark",
    "UNRESOLVED_MXN",
    "TIIE_PERIOD_DAYS",
    "TIIE_DAY_COUNT",
    "MXNCurveResult",
    "BenchmarkComparison",
    "tiie_schedule",
    "tiie_par_swap_node",
    "bootstrap_mxn_curve",
    "compare_benchmarks",
]

TIIE_PERIOD_DAYS = 28
"""The assumed TIIE coupon period, in calendar days. Unverified — see
:data:`UNRESOLVED_MXN`."""

TIIE_DAY_COUNT = DayCount.ACT_360
"""The assumed TIIE accrual basis. Unverified — see :data:`UNRESOLVED_MXN`."""

UNRESOLVED_MXN: tuple[tuple[str, str], ...] = (
    (
        "tiie_day_count",
        f"ACT/360 assumed for TIIE accrual; not confirmed against Banxico. "
        f"Currently {TIIE_DAY_COUNT.value}.",
    ),
    (
        "tiie_period_days",
        f"A {TIIE_PERIOD_DAYS}-day coupon period assumed; not confirmed against "
        "Banxico.",
    ),
    (
        "tiie_fondeo_vs_28",
        "Which conventions attach to TIIE de Fondeo as against TIIE 28 is not "
        "established. This build applies the same ones to both, which is an "
        "assumption and possibly a wrong one.",
    ),
    (
        "banxico_series_ids",
        "The Banxico SIE series identifiers for either benchmark are not known "
        "here, so fixings must be supplied by the caller rather than fetched.",
    ),
    (
        "mxn_calendar_is_bmv_not_banxico",
        "Business days come from the BMV (stock exchange) calendar. Banxico's "
        "banking calendar is a different list and the two have not been diffed.",
    ),
)
"""Every MXN convention this build assumes rather than knows.

A tuple of ``(name, why)``. Each one becomes a
:class:`~rates_engine.evidence.Degradation` on every result, and
``strict_conventions=True`` turns the set into a refusal. When Banxico
becomes reachable this tuple shrinks and nothing else changes.
"""


class TIIEBenchmark(StrEnum):
    """Which peso benchmark a curve is built on.

    ``TIIE_28``
        The historical 28-day interbank rate.
    ``TIIE_FONDEO``
        The overnight funding rate that replaced it as the reference.

    Both are supported, marked, and never mixed silently — PRD-003 decision
    1. Supporting only one would make AC-1.3, which asks the payload to
    distinguish them, vacuous.
    """

    TIIE_28 = "TIIE28"
    TIIE_FONDEO = "TIIE_FONDEO"


def tiie_schedule(start: date, periods: int, *, roll: bool = True) -> tuple[date, ...]:
    """Successive TIIE period end dates from ``start``.

    Args:
        start: First accrual start.
        periods: How many periods to generate, positive.
        roll: Adjust each end onto a BMV business day, following. Turning it
            off gives the unadjusted 28-day grid, which is what a test that
            wants to isolate the roll uses.

    Returns:
        ``periods`` dates, the last being the schedule's maturity.

    Raises:
        ValueError: ``periods`` is not positive.
    """
    if periods <= 0:
        raise ValueError(f"a schedule needs at least one period, got {periods!r}")
    out: list[date] = []
    current = start
    for _ in range(periods):
        current = current + timedelta(days=TIIE_PERIOD_DAYS)
        out.append(
            BMV.adjust(current, BusinessDayConvention.FOLLOWING) if roll else current
        )
    return tuple(out)


def tiie_par_swap_node(
    as_of: date,
    periods: int,
    quoted_rate: float,
    *,
    label: str = "tiie_par",
    roll: bool = True,
) -> ParSwapNode:
    """A par TIIE swap node with this module's assumed conventions applied.

    This is where :data:`TIIE_PERIOD_DAYS` and :data:`TIIE_DAY_COUNT` are
    actually *used*. Without it they were two constants that the payload
    advertised and no calculation ever touched — a field that reads as a
    statement about the accrual while describing nothing, which is the one
    thing this package is not allowed to do.

    A caller who builds a :class:`~rates_engine.curves.bootstrap.ParSwapNode`
    by hand supplies their own year fractions and is not bound by either
    constant; :meth:`MXNCurveResult.payload_fields` says so rather than
    claiming the conventions applied to whatever it was given.

    Args:
        as_of: Start of the first accrual period.
        periods: Number of coupon periods, positive.
        quoted_rate: The par rate as a decimal.
        label: Name for the evidence record.
        roll: Adjust period ends onto BMV business days.

    Returns:
        The node, with accruals on :data:`TIIE_DAY_COUNT`.
    """
    payments = tiie_schedule(as_of, periods, roll=roll)
    starts = (as_of, *payments[:-1])
    return ParSwapNode(
        start=as_of,
        payment_dates=payments,
        year_fractions=tuple(
            year_fraction(start, end, TIIE_DAY_COUNT)
            for start, end in zip(starts, payments, strict=True)
        ),
        quoted_rate=quoted_rate,
        label=label,
    )


def _degradations() -> tuple[Degradation, ...]:
    return tuple(
        Degradation(
            code=f"unresolved_convention:{name}",
            message=why,
            data_quality=DataQuality.ASSUMED,
        )
        for name, why in UNRESOLVED_MXN
    )


@dataclass(frozen=True)
class MXNCurveResult(EngineResult):
    """A peso curve, the benchmark behind it, and what it had to assume.

    Attributes:
        curve: The discount curve, in MXN.
        benchmark: Which benchmark the quotes are on.
        residuals_bp: Repricing residual per instrument label.
        max_residual_bp: The worst of them.
        unresolved_conventions: The names from :data:`UNRESOLVED_MXN` that
            this result rests on. Empty only when they have been verified.
    """

    curve: DiscountCurve
    benchmark: TIIEBenchmark
    residuals_bp: dict[str, float]
    max_residual_bp: float
    unresolved_conventions: tuple[str, ...]

    def payload_fields(self) -> dict[str, Any]:
        """The curve, the benchmark, the fit, and the conventions assumed."""
        return {
            "curve": self.curve.to_dict(),
            "benchmark": self.benchmark.value,
            "currency": self.curve.currency.value,
            "residuals_bp": dict(self.residuals_bp),
            "max_residual_bp": self.max_residual_bp,
            "unresolved_conventions": list(self.unresolved_conventions),
            "tiie_period_days": TIIE_PERIOD_DAYS,
            "tiie_day_count": TIIE_DAY_COUNT.value,
            "conventions_apply_to": (
                "instruments built by tiie_par_swap_node and tiie_schedule. A "
                "ParSwapNode supplied directly carries its own year fractions, "
                "which this module neither sets nor inspects."
            ),
        }


def bootstrap_mxn_curve(
    as_of: date,
    instruments: tuple[CalibrationInstrument, ...],
    benchmark: TIIEBenchmark,
    *,
    interpolation: str = "log_linear_df",
    strict: bool = True,
    tolerance_bp: float = DEFAULT_TOLERANCE_BP,
    strict_conventions: bool = False,
) -> MXNCurveResult:
    """Bootstrap a peso curve, carrying the conventions it had to assume.

    Args:
        as_of: Valuation date.
        instruments: Calibration set — fixings, stubs and par swaps, the
            same node types the USD bootstrap takes, because the curve layer
            is currency-agnostic by construction.
        benchmark: Which peso benchmark the quotes are on. Required: a curve
            that does not say is a curve nobody can compare.
        interpolation: Curve interpolation.
        strict: Refuse when an instrument misses ``tolerance_bp``.
        strict_conventions: Refuse outright while any MXN convention is
            unverified. AC-1.4. Off by default so the machinery is usable
            and visibly marked; on for anything that must not rest on an
            assumption.

    Returns:
        The :class:`MXNCurveResult`.

    Raises:
        UnresolvedConventionError: ``strict_conventions`` is set and
            :data:`UNRESOLVED_MXN` is not empty.
        CurveArbitrageError: The instruments imply a non-monotone curve.
        BootstrapResidualError: Under ``strict``, an instrument missed.
    """
    unresolved = tuple(name for name, _ in UNRESOLVED_MXN)
    if strict_conventions and unresolved:
        raise UnresolvedConventionError(
            f"strict_conventions is set and {len(unresolved)} MXN conventions are "
            f"assumed rather than verified: {', '.join(unresolved)}. "
            "PRD-003's research gate could not reach Banxico from this environment "
            "(403 at the egress proxy), so the day count, the coupon period and the "
            "benchmark distinction are all guesses carried in the evidence. Run "
            "without strict_conventions to get a curve that says so, or resolve them "
            "and shorten UNRESOLVED_MXN."
        )

    result = bootstrap_discount_curve(
        as_of,
        instruments,
        interpolation=interpolation,
        strict=strict,
        tolerance_bp=tolerance_bp,
        currency=Currency.MXN,
    )
    evidence = Evidence(
        produced_by="curves.bootstrap_mxn_curve",
        fields={
            "benchmark": benchmark.value,
            "currency": Currency.MXN.value,
            "instruments": len(instruments),
            "max_residual_bp": max((abs(v) for v in result.residuals_bp.values()), default=0.0),
            "unresolved_conventions": list(unresolved),
            "tiie_period_days": TIIE_PERIOD_DAYS,
            "tiie_day_count": TIIE_DAY_COUNT.value,
            "conventions_apply_to": (
                "instruments built by tiie_par_swap_node and tiie_schedule. A "
                "ParSwapNode supplied directly carries its own year fractions, "
                "which this module neither sets nor inspects."
            ),
            "calendar": BMV.name,
            "note": (
                "Every convention named in unresolved_conventions is assumed, not "
                "verified. Anything priced on this curve inherits ASSUMED quality."
            ),
        },
        sources=(result.evidence,),
        warnings=_degradations(),
    )
    return MXNCurveResult(
        evidence=evidence,
        curve=result.curve,
        benchmark=benchmark,
        residuals_bp=dict(result.residuals_bp),
        max_residual_bp=max((abs(v) for v in result.residuals_bp.values()), default=0.0),
        unresolved_conventions=unresolved,
    )


@dataclass(frozen=True)
class BenchmarkComparison(EngineResult):
    """Two peso curves on different benchmarks, and the gap between them.

    Attributes:
        first: The first curve's result.
        second: The second's.
        zero_difference_bp: Zero rate of ``second`` minus that of ``first``,
            in basis points, at each sampled node.
        max_difference_bp: The largest of those in absolute value.
        sample_dates: The dates sampled.
    """

    first: MXNCurveResult
    second: MXNCurveResult
    zero_difference_bp: tuple[float, ...]
    max_difference_bp: float
    sample_dates: tuple[date, ...]

    def payload_fields(self) -> dict[str, Any]:
        """Both benchmarks named, and the difference they imply."""
        return {
            "first_benchmark": self.first.benchmark.value,
            "second_benchmark": self.second.benchmark.value,
            "zero_difference_bp": list(self.zero_difference_bp),
            "max_difference_bp": self.max_difference_bp,
            "sample_dates": [d.isoformat() for d in self.sample_dates],
            "unresolved_conventions": list(self.first.unresolved_conventions),
        }


def compare_benchmarks(first: MXNCurveResult, second: MXNCurveResult) -> BenchmarkComparison:
    """The zero-rate gap between two peso curves on different benchmarks.

    AC-1.3. The comparison exists so that using one benchmark where the
    other was meant is a visible difference rather than an invisible one.

    Args:
        first: One curve.
        second: The other, on a different benchmark.

    Returns:
        The :class:`BenchmarkComparison`.

    Raises:
        ValueError: Both results are on the same benchmark, which is not a
            comparison, or their valuation dates differ.
    """
    if first.benchmark is second.benchmark:
        raise ValueError(
            f"both curves are on {first.benchmark.value}; comparing a benchmark with "
            "itself is not what AC-1.3 asks for. The point is that the two are "
            "different and the payload says by how much."
        )
    if first.curve.as_of != second.curve.as_of:
        raise ValueError(
            f"curves are as of {first.curve.as_of} and {second.curve.as_of}; a "
            "benchmark difference read across two dates is a benchmark difference "
            "plus a market move"
        )

    shared = [d for d in first.curve.nodes if d <= second.curve.nodes[-1]]
    differences = tuple(
        (second.curve.zero(day) - first.curve.zero(day)) * 1e4 for day in shared
    )
    worst = max((abs(v) for v in differences), default=0.0)
    evidence = Evidence(
        produced_by="curves.compare_benchmarks",
        fields={
            "first_benchmark": first.benchmark.value,
            "second_benchmark": second.benchmark.value,
            "max_difference_bp": worst,
            "sampled_nodes": len(shared),
            "unresolved_conventions": list(first.unresolved_conventions),
            "note": (
                "TIIE 28 and TIIE de Fondeo are different rates. This is the gap "
                "between curves built on each, reported so that using one where the "
                "other was meant is visible."
            ),
        },
        sources=(first.evidence, second.evidence),
        warnings=_degradations(),
    )
    return BenchmarkComparison(
        evidence=evidence,
        first=first,
        second=second,
        zero_difference_bp=differences,
        max_difference_bp=worst,
        sample_dates=tuple(shared),
    )
