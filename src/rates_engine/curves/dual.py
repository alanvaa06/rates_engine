"""Solving the OIS and tenor curves together, against synthetic inputs.

**Why the inputs are synthetic.** A dual-curve calibration needs par quotes on
a term index and basis swap spreads against OIS. Neither is available free:
ICE Swap Rate and the commercial CME Term SOFR feeds are licensed. So v1
validates the *solver* rather than a market — the inputs are constructed with
a known basis, and the tests assert the properties that must hold whatever the
basis is. Asking for a real quote source raises
:class:`~rates_engine.errors.NoTenorQuoteSourceError` rather than quietly
substituting something plausible, and every result declares
``inputs_origin="synthetic"`` so a reader cannot mistake it for a market fit.

**Sequential against simultaneous.** Sequential solves the OIS curve first and
then the tenor curve with the OIS curve held fixed; simultaneous solves both
at once. They agree exactly when the basis is zero, because then the tenor
curve is the OIS curve and there is nothing to feed back. With a wide basis
they can differ by a few basis points in the long end, and the difference per
tenor is reported rather than argued about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable

import numpy as np
from scipy.optimize import least_squares

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.bootstrap import (
    CalibrationInstrument,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import DiscountCurve
from rates_engine.errors import NoTenorQuoteSourceError, UnderdeterminedCurveError
from rates_engine.evidence import DataQuality, Evidence, Provenance
from rates_engine.results import EngineResult

__all__ = [
    "DualCalibrationInstrument",
    "TenorParSwapNode",
    "BasisSwapNode",
    "DualCurveResult",
    "solve_dual_curve",
    "SYNTHETIC",
]

SYNTHETIC = "synthetic"
"""The only accepted ``inputs_origin`` in v1."""

_SYNTHETIC_PROVENANCE = Provenance(
    source="synthetic",
    instrument_kind="dual_curve_quote",
    data_quality=DataQuality.SYNTHETIC,
    notes="Constructed input. v1 has no free source of term or basis quotes.",
)


@runtime_checkable
class DualCalibrationInstrument(Protocol):
    """A quote whose residual needs both curves at once."""

    label: str
    provenance: Provenance

    @property
    def node_date(self) -> date:
        """The tenor-curve node this instrument pins."""
        ...

    def residual_bp(self, ois: DiscountCurve, tenor: DiscountCurve) -> float:
        """Model minus quote, in basis points."""
        ...

    def describe(self) -> dict[str, Any]:
        """Terms of the quote, for the evidence record."""
        ...


@dataclass(frozen=True)
class TenorParSwapNode:
    """A fixed-versus-term-index par swap: projected on tenor, discounted on OIS.

    Attributes:
        start: Float leg start.
        payment_dates: Fixed leg payment dates, increasing.
        year_fractions: Fixed leg accruals, aligned with the payments.
        float_periods: ``(start, end)`` of each floating period.
        quoted_rate: Par rate as a decimal.
        label: Name for the evidence record.
        provenance: Where the quote came from.
        day_count: Basis for the floating accruals.
    """

    start: date
    payment_dates: tuple[date, ...]
    year_fractions: tuple[float, ...]
    float_periods: tuple[tuple[date, date], ...]
    quoted_rate: float
    label: str = "tenor_par_swap"
    provenance: Provenance = field(default_factory=lambda: _SYNTHETIC_PROVENANCE)
    day_count: DayCount = DayCount.ACT_360

    @property
    def node_date(self) -> date:
        """The final fixed payment date."""
        return self.payment_dates[-1]

    def par_rate(self, ois: DiscountCurve, tenor: DiscountCurve) -> float:
        """Par rate under these two curves, as a decimal."""
        annuity = sum(
            tau * ois.df(pay) for tau, pay in zip(self.year_fractions, self.payment_dates, strict=True)
        )
        floating = sum(
            tenor.forward(begin, end, day_count=self.day_count)
            * year_fraction(begin, end, self.day_count)
            * ois.df(end)
            for begin, end in self.float_periods
        )
        return floating / annuity

    def residual_bp(self, ois: DiscountCurve, tenor: DiscountCurve) -> float:
        """Model par rate minus quoted par rate, in basis points."""
        return (self.par_rate(ois, tenor) - self.quoted_rate) * 1e4

    def describe(self) -> dict[str, Any]:
        """Terms of the tenor par swap."""
        return {
            "kind": "tenor_par_swap",
            "start": self.start.isoformat(),
            "maturity": self.node_date.isoformat(),
            "quoted_rate": self.quoted_rate,
            "float_periods": len(self.float_periods),
            "day_count": self.day_count.value,
        }


@dataclass(frozen=True)
class BasisSwapNode:
    """An OIS-versus-term basis swap, quoted as a spread on the OIS leg.

    Attributes:
        float_periods: ``(start, end)`` of each period, shared by both legs.
        quoted_spread: Par spread as a decimal, so 5 bp is ``0.0005``.
        label: Name for the evidence record.
        provenance: Where the quote came from.
        day_count: Basis for the accruals.
    """

    float_periods: tuple[tuple[date, date], ...]
    quoted_spread: float
    label: str = "basis_swap"
    provenance: Provenance = field(default_factory=lambda: _SYNTHETIC_PROVENANCE)
    day_count: DayCount = DayCount.ACT_360

    @property
    def node_date(self) -> date:
        """The last period end."""
        return self.float_periods[-1][1]

    def par_spread(self, ois: DiscountCurve, tenor: DiscountCurve) -> float:
        """The spread that prices the basis swap at zero, as a decimal.

        The difference of the two projected forwards, annuity-weighted. This
        is Bianchetti's forward basis aggregated to a single quote.
        """
        numerator = 0.0
        annuity = 0.0
        for begin, end in self.float_periods:
            tau = year_fraction(begin, end, self.day_count)
            weight = tau * ois.df(end)
            numerator += (
                tenor.forward(begin, end, day_count=self.day_count)
                - ois.forward(begin, end, day_count=self.day_count)
            ) * weight
            annuity += weight
        return numerator / annuity

    def residual_bp(self, ois: DiscountCurve, tenor: DiscountCurve) -> float:
        """Model par spread minus quoted spread, in basis points."""
        return (self.par_spread(ois, tenor) - self.quoted_spread) * 1e4

    def describe(self) -> dict[str, Any]:
        """Terms of the basis swap."""
        return {
            "kind": "basis_swap",
            "maturity": self.node_date.isoformat(),
            "quoted_spread": self.quoted_spread,
            "periods": len(self.float_periods),
            "day_count": self.day_count.value,
        }


@dataclass(frozen=True)
class DualCurveResult(EngineResult):
    """Both curves, their residuals, and how the two solve modes compare.

    Attributes:
        ois: The discount curve.
        tenor: The projection curve.
        mode: ``"simultaneous"`` or ``"sequential"``.
        residuals_bp: Final residual per instrument label, in basis points.
        forward_basis_bp: Tenor forward minus OIS forward per tenor node, bp.
        sequential_vs_simultaneous_bp: Difference in the tenor curve's zero
            rate per node between the two modes, in basis points, when both
            were run. ``None`` otherwise.
        inputs_origin: Always ``"synthetic"`` in v1.
    """

    ois: DiscountCurve
    tenor: DiscountCurve
    mode: str
    residuals_bp: dict[str, float]
    forward_basis_bp: dict[str, float]
    sequential_vs_simultaneous_bp: dict[str, float] | None
    inputs_origin: str

    def payload_fields(self) -> dict[str, Any]:
        """Both curves and every diagnostic the comparison produces."""
        return {
            "mode": self.mode,
            "inputs_origin": self.inputs_origin,
            "ois_curve": self.ois.to_dict(),
            "tenor_curve": self.tenor.to_dict(),
            "residuals_bp": dict(self.residuals_bp),
            "max_abs_residual_bp": max(
                (abs(r) for r in self.residuals_bp.values()), default=0.0
            ),
            "forward_basis_bp": dict(self.forward_basis_bp),
            "sequential_vs_simultaneous_bp": (
                dict(self.sequential_vs_simultaneous_bp)
                if self.sequential_vs_simultaneous_bp is not None
                else None
            ),
        }


def solve_dual_curve(
    as_of: date,
    ois_instruments: tuple[CalibrationInstrument, ...],
    tenor_instruments: tuple[DualCalibrationInstrument, ...],
    basis_instruments: tuple[DualCalibrationInstrument, ...],
    *,
    mode: str = "simultaneous",
    inputs_origin: str = SYNTHETIC,
    compare_modes: bool = True,
    tolerance_bp: float = 0.01,
) -> DualCurveResult:
    """Solve the OIS and tenor curves from OIS, term and basis quotes.

    Args:
        as_of: Valuation date.
        ois_instruments: Single-curve quotes pinning the OIS curve.
        tenor_instruments: Term par swaps pinning the tenor curve.
        basis_instruments: Basis swap spreads pinning the tenor curve.
        mode: ``"simultaneous"`` solves both curves at once;
            ``"sequential"`` solves OIS first and holds it fixed.
        inputs_origin: Must be ``"synthetic"``. v1 has no real quote source.
        compare_modes: Also run the other mode and report the per-node
            difference in the tenor curve's zero rates.
        tolerance_bp: Residual tolerance used for the report, in basis points.

    Returns:
        The :class:`DualCurveResult`.

    Raises:
        NoTenorQuoteSourceError: ``inputs_origin`` is anything but
            ``"synthetic"``.
        UnderdeterminedCurveError: There are more tenor-curve nodes than
            instruments to pin them, naming the nodes left without a quote.
        ValueError: ``mode`` is not one of the two.
    """
    if inputs_origin != SYNTHETIC:
        raise NoTenorQuoteSourceError(
            f"inputs_origin={inputs_origin!r} asks for real term or basis quotes. "
            "v1 has no free source of Term SOFR par rates or OIS-versus-term basis "
            "spreads, so the solver is validated against synthetic inputs only; pass "
            f"inputs_origin={SYNTHETIC!r}. A real provider arrives in v1.1."
        )
    if mode not in ("simultaneous", "sequential"):
        raise ValueError(f"mode must be 'simultaneous' or 'sequential', got {mode!r}")

    dual = tuple(tenor_instruments) + tuple(basis_instruments)
    if not dual:
        raise UnderdeterminedCurveError(
            "no tenor or basis instruments: the projection curve has nothing to pin it"
        )
    node_dates = sorted({i.node_date for i in dual})
    if len(node_dates) > len(dual):  # pragma: no cover - guarded by the set above
        raise UnderdeterminedCurveError("more tenor nodes than instruments")
    if len(dual) > len(node_dates):
        counts: dict[date, list[str]] = {}
        for instrument in dual:
            counts.setdefault(instrument.node_date, []).append(instrument.label)
        overloaded = {d: labels for d, labels in counts.items() if len(labels) > 1}
        raise UnderdeterminedCurveError(
            "these tenor nodes carry more than one instrument, so at least one node "
            f"has none of its own: "
            f"{ {node.isoformat(): labels for node, labels in overloaded.items()} }"
        )

    ois_result = bootstrap_discount_curve(as_of, ois_instruments)
    ois_curve = ois_result.curve

    sequential = _solve_tenor(as_of, ois_curve, dual, node_dates)
    if mode == "simultaneous":
        ois_curve_final, tenor_curve = _solve_simultaneous(
            as_of, ois_instruments, ois_curve, dual, node_dates, sequential
        )
    else:
        ois_curve_final, tenor_curve = ois_curve, sequential

    comparison: dict[str, float] | None = None
    if compare_modes:
        other = (
            sequential
            if mode == "simultaneous"
            else _solve_simultaneous(
                as_of, ois_instruments, ois_curve, dual, node_dates, sequential
            )[1]
        )
        comparison = {
            node.isoformat(): (tenor_curve.zero(node) - other.zero(node)) * 1e4
            for node in tenor_curve.nodes
        }

    residuals = {i.label: i.residual_bp(ois_curve_final, tenor_curve) for i in dual}
    forward_basis = {
        f"{begin.isoformat()}:{end.isoformat()}": (
            tenor_curve.forward(begin, end) - ois_curve_final.forward(begin, end)
        )
        * 1e4
        for begin, end in zip((as_of, *tenor_curve.nodes[:-1]), tenor_curve.nodes, strict=True)
    }

    evidence = Evidence(
        produced_by="curves.solve_dual_curve",
        inputs=tuple(i.provenance for i in dual),
        fields={
            "mode": mode,
            "inputs_origin": inputs_origin,
            "inputs_origin_note": (
                "Synthetic by decision: no free source of Term SOFR par or basis "
                "quotes exists, so v1 validates the solver rather than a market fit."
            ),
            "tolerance_bp": tolerance_bp,
            "instruments_used": [
                {"label": i.label, **i.describe(), "residual_bp": residuals[i.label]}
                for i in dual
            ],
            "fit_residuals_bp": dict(residuals),
            "max_abs_residual_bp": max((abs(r) for r in residuals.values()), default=0.0),
            "tenor_nodes": [n.isoformat() for n in tenor_curve.nodes],
            "forward_basis_bp": dict(forward_basis),
            "sequential_vs_simultaneous_bp": comparison,
        },
        sources=(ois_result.evidence,),
    )
    return DualCurveResult(
        evidence=evidence,
        ois=ois_curve_final,
        tenor=tenor_curve,
        mode=mode,
        residuals_bp=residuals,
        forward_basis_bp=forward_basis,
        sequential_vs_simultaneous_bp=comparison,
        inputs_origin=inputs_origin,
    )


def _initial_tenor(as_of: date, ois: DiscountCurve, node_dates: list[date]) -> DiscountCurve:
    """Start the tenor curve on top of the OIS curve: the zero-basis answer."""
    return DiscountCurve(as_of, tuple(node_dates), tuple(ois.df(d) for d in node_dates))


def _solve_tenor(
    as_of: date,
    ois: DiscountCurve,
    dual: tuple[DualCalibrationInstrument, ...],
    node_dates: list[date],
) -> DiscountCurve:
    """Solve the tenor curve with the OIS curve held fixed, all nodes at once."""
    start = _initial_tenor(as_of, ois, node_dates)
    by_node = {i.node_date: i for i in dual}
    ordered = [by_node[d] for d in node_dates]
    guess = np.array([math.log(df) for df in start.dfs])

    def residuals(logs: np.ndarray) -> np.ndarray:
        curve = DiscountCurve(as_of, tuple(node_dates), tuple(float(math.exp(x)) for x in logs))
        return np.array([i.residual_bp(ois, curve) for i in ordered])

    solution = least_squares(residuals, guess, xtol=1e-15, ftol=1e-15, gtol=1e-15)
    return DiscountCurve(
        as_of, tuple(node_dates), tuple(float(math.exp(x)) for x in solution.x)
    )


def _solve_simultaneous(
    as_of: date,
    ois_instruments: tuple[CalibrationInstrument, ...],
    ois_start: DiscountCurve,
    dual: tuple[DualCalibrationInstrument, ...],
    node_dates: list[date],
    tenor_start: DiscountCurve,
) -> tuple[DiscountCurve, DiscountCurve]:
    """Solve every OIS and tenor node together against every residual at once."""
    ois_nodes = ois_start.nodes
    split = len(ois_nodes)
    by_node = {i.node_date: i for i in dual}
    ordered = [by_node[d] for d in node_dates]
    guess = np.array(
        [math.log(df) for df in ois_start.dfs] + [math.log(df) for df in tenor_start.dfs]
    )

    def residuals(logs: np.ndarray) -> np.ndarray:
        ois = DiscountCurve(
            as_of, ois_nodes, tuple(float(math.exp(x)) for x in logs[:split])
        )
        tenor = DiscountCurve(
            as_of, tuple(node_dates), tuple(float(math.exp(x)) for x in logs[split:])
        )
        return np.array(
            [i.residual_bp(ois) for i in ois_instruments]
            + [i.residual_bp(ois, tenor) for i in ordered]
        )

    solution = least_squares(residuals, guess, xtol=1e-15, ftol=1e-15, gtol=1e-15)
    return (
        DiscountCurve(as_of, ois_nodes, tuple(float(math.exp(x)) for x in solution.x[:split])),
        DiscountCurve(
            as_of, tuple(node_dates), tuple(float(math.exp(x)) for x in solution.x[split:])
        ),
    )
