"""Sequential bootstrap: one instrument, one node, solved in maturity order.

Every calibration instrument exposes the same two things — the node it pins
and the residual it leaves — so the solver is one loop rather than a branch
per instrument type. That uniformity is what makes AC-3.1 fall out for free:
if each instrument is solved to a zero residual, every instrument reprices.

**The proxy gate.** Treasury par yields standing in for OIS par are allowed and
never silent. An instrument whose provenance says ``PROXY`` is refused unless
the caller passed ``long_end_source="treasury_proxy"``; once declared, the
nodes it pins are marked, a degradation is recorded, and both travel up the
evidence chain into whatever consumes the curve.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable

from scipy.optimize import brentq

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.discount import DiscountCurve
from rates_engine.errors import (
    BootstrapResidualError,
    CurveArbitrageError,
    ProxySourceNotDeclaredError,
    UnderdeterminedCurveError,
)
from rates_engine.evidence import DataQuality, Degradation, Evidence, Provenance
from rates_engine.money import Currency
from rates_engine.results import EngineResult

__all__ = [
    "CalibrationInstrument",
    "RealizedStubNode",
    "FuturesNode",
    "ParSwapNode",
    "BootstrapResult",
    "bootstrap_discount_curve",
    "TREASURY_PROXY",
    "DEFAULT_TOLERANCE_BP",
]

TREASURY_PROXY = "treasury_proxy"
"""The only accepted value of ``long_end_source``; there is no default."""

DEFAULT_TOLERANCE_BP = 0.01
"""Residual a calibration instrument must reprice within, in basis points."""

_SOLVER_XTOL = 1e-16
_SOLVER_RTOL = 8.9e-16


@runtime_checkable
class CalibrationInstrument(Protocol):
    """What the bootstrap needs from anything it calibrates to."""

    label: str
    provenance: Provenance

    @property
    def node_date(self) -> date:
        """The single curve node this instrument pins."""
        ...

    def residual_bp(self, curve: DiscountCurve) -> float:
        """Quoted minus model, in basis points, signed. Zero when it reprices."""
        ...

    def describe(self) -> dict[str, Any]:
        """Instrument terms for the evidence record."""
        ...


@dataclass(frozen=True)
class RealizedStubNode:
    """The accrual already realised between the valuation date and the first node.

    Attributes:
        end: Node date the stub pins.
        accrual_factor: Compounded growth over the stub, ``1 + r * tau``.
        label: Name for the evidence record.
        provenance: Where the fixings came from.
    """

    end: date
    accrual_factor: float
    label: str = "realized_stub"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="derived", instrument_kind="realized_stub")
    )

    @property
    def node_date(self) -> date:
        """The stub's end date."""
        return self.end

    def residual_bp(self, curve: DiscountCurve) -> float:
        """Model growth minus realised growth, expressed in basis points of rate."""
        tau = year_fraction(curve.as_of, self.end, DayCount.ACT_360)
        if tau <= 0.0:
            return 0.0
        model = (curve.df(curve.as_of) / curve.df(self.end) - 1.0) / tau
        realised = (self.accrual_factor - 1.0) / tau
        return (model - realised) * 1e4

    def describe(self) -> dict[str, Any]:
        """Terms of the stub."""
        return {
            "kind": "realized_stub",
            "end": self.end.isoformat(),
            "accrual_factor": self.accrual_factor,
        }


@dataclass(frozen=True)
class FuturesNode:
    """A futures contract's convexity-adjusted forward, pinning its period end.

    Attributes:
        start: Reference period start.
        end: Reference period end, the node this pins.
        forward_rate: Convexity-adjusted forward as a decimal, ACT/360.
        label: Name for the evidence record.
        provenance: Where the settlement price came from.
        convexity: Model, sigma, kappa and adjustment in bp, for the evidence.
    """

    start: date
    end: date
    forward_rate: float
    label: str = "future"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="file", instrument_kind="futures_settlement")
    )
    convexity: dict[str, Any] = field(default_factory=dict)

    @property
    def node_date(self) -> date:
        """The reference period end."""
        return self.end

    def residual_bp(self, curve: DiscountCurve) -> float:
        """Curve-implied forward minus the adjusted forward, in basis points."""
        implied = curve.forward(self.start, self.end, day_count=DayCount.ACT_360)
        return (implied - self.forward_rate) * 1e4

    def describe(self) -> dict[str, Any]:
        """Terms of the futures node, including how its forward was adjusted."""
        return {
            "kind": "future",
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "forward_rate": self.forward_rate,
            "convexity": dict(self.convexity) or None,
        }


@dataclass(frozen=True)
class ParSwapNode:
    """A par swap quote, pinning the node at its final payment.

    Holds dates and year fractions rather than a swap object on purpose: the
    curve layer does not import the instrument layer, so a bootstrap cannot
    accidentally depend on how a product happens to be modelled.

    Attributes:
        start: Float leg start; the float leg telescopes to
            ``P(start) - P(end)``.
        payment_dates: Fixed leg payment dates, increasing.
        year_fractions: Fixed leg accruals in years, aligned with the payments.
        quoted_rate: The par rate as a decimal.
        label: Name for the evidence record.
        provenance: Where the quote came from. A ``PROXY`` quality here is what
            trips the ``long_end_source`` gate.
    """

    start: date
    payment_dates: tuple[date, ...]
    year_fractions: tuple[float, ...]
    quoted_rate: float
    label: str = "par_swap"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="file", instrument_kind="ois_par")
    )

    def __post_init__(self) -> None:
        if len(self.payment_dates) != len(self.year_fractions):
            raise ValueError(
                f"{self.label}: {len(self.payment_dates)} payments and "
                f"{len(self.year_fractions)} accruals"
            )
        if not self.payment_dates:
            raise ValueError(f"{self.label}: a par swap needs at least one payment")

    @property
    def node_date(self) -> date:
        """The final payment date."""
        return self.payment_dates[-1]

    def annuity(self, curve: DiscountCurve) -> float:
        """Fixed leg annuity per unit notional, in years of discounted accrual."""
        return sum(tau * curve.df(pay) for tau, pay in zip(self.year_fractions, self.payment_dates, strict=True))

    def par_rate(self, curve: DiscountCurve) -> float:
        """The rate that makes this swap price at zero on ``curve``, as a decimal."""
        annuity = self.annuity(curve)
        if annuity <= 0.0:
            raise CurveArbitrageError(
                f"{self.label}: annuity is {annuity!r}; the curve cannot support a par rate"
            )
        return (curve.df(self.start) - curve.df(self.node_date)) / annuity

    def residual_bp(self, curve: DiscountCurve) -> float:
        """Model par rate minus quoted par rate, in basis points."""
        return (self.par_rate(curve) - self.quoted_rate) * 1e4

    def describe(self) -> dict[str, Any]:
        """Terms of the par swap node."""
        return {
            "kind": "par_swap",
            "start": self.start.isoformat(),
            "maturity": self.node_date.isoformat(),
            "payments": len(self.payment_dates),
            "quoted_rate": self.quoted_rate,
        }


@dataclass(frozen=True)
class BootstrapResult(EngineResult):
    """A discount curve and the record of what pinned each node.

    Attributes:
        curve: The bootstrapped curve.
        residuals_bp: Final repricing residual per instrument label, in bp.
        node_quality: Data quality per curve node, aligned with ``curve.nodes``.
            A node pinned by a Treasury proxy is ``PROXY`` here.
        dropped_instruments: Instruments excluded, each with the reason.
        long_end_source: What was declared for the long end, or ``None``.
    """

    curve: DiscountCurve
    residuals_bp: dict[str, float]
    node_quality: tuple[DataQuality, ...]
    dropped_instruments: tuple[dict[str, Any], ...]
    long_end_source: str | None

    @property
    def uses_proxy(self) -> bool:
        """True when any node rests on a proxy quote."""
        return DataQuality.PROXY in self.node_quality

    def payload_fields(self) -> dict[str, Any]:
        """Curve, residuals, per-node quality and the declared long-end source."""
        return {
            "curve": self.curve.to_dict(),
            "residuals_bp": dict(self.residuals_bp),
            "node_quality": [q.value for q in self.node_quality],
            "dropped_instruments": [dict(d) for d in self.dropped_instruments],
            "long_end_source": self.long_end_source,
        }


def bootstrap_discount_curve(
    as_of: date,
    instruments: tuple[CalibrationInstrument, ...],
    *,
    interpolation: str = "log_linear_df",
    long_end_source: str | None = None,
    strict: bool = True,
    tolerance_bp: float = DEFAULT_TOLERANCE_BP,
    currency: Currency = Currency.USD,
) -> BootstrapResult:
    """Solve one curve node per instrument, in maturity order.

    Args:
        as_of: Valuation date. ``df(as_of)`` is 1 by construction.
        instruments: Calibration instruments. Sorted here by node date, so the
            caller's ordering does not change the answer.
        interpolation: Curve interpolation; ``"log_linear_df"`` in v1.
        long_end_source: Must be ``"treasury_proxy"`` to admit proxy-quality
            instruments. ``None`` means no proxy is accepted, and there is no
            default that quietly admits one.
        strict: When true, an instrument left outside ``tolerance_bp`` raises.
            When false it is recorded in ``dropped_instruments`` instead, and
            is never simply discarded.
        tolerance_bp: Residual tolerance in basis points.
        currency: What the resulting curve discounts. Defaults to USD,
            which every v1 and v2 curve already was.

    Returns:
        The :class:`BootstrapResult`.

    Raises:
        UnderdeterminedCurveError: No instruments, or two instruments pinning
            the same node date, which leaves a node without its own quote.
        ProxySourceNotDeclaredError: A proxy instrument was supplied without
            ``long_end_source="treasury_proxy"``.
        CurveArbitrageError: An instrument implies a non-decreasing discount
            factor, so no admissible node solves it.
        BootstrapResidualError: Under ``strict``, an instrument does not
            reprice within tolerance.
    """
    if not instruments:
        raise UnderdeterminedCurveError(
            "no calibration instruments: a curve with no quotes is not underdetermined, "
            "it is undefined"
        )
    ordered = sorted(instruments, key=lambda inst: inst.node_date)
    seen: dict[date, str] = {}
    for inst in ordered:
        if inst.node_date in seen:
            raise UnderdeterminedCurveError(
                f"instruments {seen[inst.node_date]!r} and {inst.label!r} both pin the node "
                f"{inst.node_date}; one node needs one instrument"
            )
        seen[inst.node_date] = inst.label

    proxies = [i for i in ordered if i.provenance.data_quality is DataQuality.PROXY]
    if proxies and long_end_source != TREASURY_PROXY:
        raise ProxySourceNotDeclaredError(
            "instruments "
            + ", ".join(repr(i.label) for i in proxies)
            + " carry proxy-quality quotes; pass long_end_source="
            f"{TREASURY_PROXY!r} to use them. There is no default that admits a proxy."
        )
    if long_end_source is not None and long_end_source != TREASURY_PROXY:
        raise ProxySourceNotDeclaredError(
            f"long_end_source {long_end_source!r} is not recognised; "
            f"the only accepted value is {TREASURY_PROXY!r}"
        )

    curve = DiscountCurve(
        as_of, (ordered[0].node_date,), (1.0,), interpolation, currency=currency
    )
    curve = _solve_node(curve, ordered[0], previous_df=1.0)
    for instrument in ordered[1:]:
        curve = _solve_node(
            curve.with_node(instrument.node_date, curve.dfs[-1]),
            instrument,
            previous_df=curve.dfs[-1],
        )

    # AC-3.2: the curve a calibration produces must be monotone. A bumped
    # curve need not be, which is why the check lives here and not in the
    # DiscountCurve constructor.
    curve.require_monotone("bootstrap_discount_curve")

    residuals = {inst.label: inst.residual_bp(curve) for inst in ordered}
    dropped: list[dict[str, Any]] = []
    for instrument in ordered:
        residual = residuals[instrument.label]
        if abs(residual) <= tolerance_bp:
            continue
        reason = (
            f"residual {residual:.6f} bp exceeds tolerance {tolerance_bp} bp "
            "after solving its node"
        )
        if strict:
            raise BootstrapResidualError(f"{instrument.label}: {reason}")
        dropped.append({"label": instrument.label, "reason": reason, "residual_bp": residual})

    node_quality = tuple(inst.provenance.data_quality for inst in ordered)
    warnings: list[Degradation] = []
    if long_end_source == TREASURY_PROXY and proxies:
        warnings.append(
            Degradation(
                code="treasury_par_proxy",
                message=(
                    "The long end rests on Treasury par yields standing in for OIS par. "
                    "The swap spread they carry is negative and variable, and the bias in "
                    "these discount factors is known to exist and is not quantified. "
                    "Affected nodes: "
                    + ", ".join(i.node_date.isoformat() for i in proxies)
                ),
                data_quality=DataQuality.PROXY,
            )
        )

    evidence = Evidence(
        produced_by="curves.bootstrap_discount_curve",
        inputs=tuple(inst.provenance for inst in ordered),
        fields={
            "interpolation": interpolation,
            "tolerance_bp": tolerance_bp,
            "strict": strict,
            "long_end_source": long_end_source,
            "instruments_used": [
                {"label": i.label, **i.describe(), "residual_bp": residuals[i.label]}
                for i in ordered
            ],
            "fit_residuals_bp": dict(residuals),
            "dropped_instruments": [dict(d) for d in dropped] or None,
            "max_abs_residual_bp": max((abs(r) for r in residuals.values()), default=0.0),
            "nodes": [n.isoformat() for n in curve.nodes],
            "convexity_models": sorted(_convexity_models(ordered)) or None,
        },
        warnings=tuple(warnings),
    )
    return BootstrapResult(
        evidence=evidence,
        curve=curve,
        residuals_bp=residuals,
        node_quality=node_quality,
        dropped_instruments=tuple(dropped),
        long_end_source=long_end_source,
    )


def _convexity_models(instruments: Sequence[CalibrationInstrument]) -> set[str]:
    """Convexity models named by any instrument that carries the metadata.

    Optional by design: a par swap node has no convexity model, and reading
    the attribute rather than requiring it on the protocol keeps that from
    becoming a field every instrument has to declare empty.
    """
    models: set[str] = set()
    for instrument in instruments:
        metadata = getattr(instrument, "convexity", None)
        if isinstance(metadata, dict) and metadata.get("model"):
            models.add(str(metadata["model"]))
    return models


def _solve_node(
    curve: DiscountCurve, instrument: CalibrationInstrument, *, previous_df: float
) -> DiscountCurve:
    """Solve the last node's discount factor so ``instrument`` reprices exactly."""
    node = instrument.node_date

    def residual(df: float) -> float:
        return instrument.residual_bp(curve.with_node(node, df))

    upper = previous_df
    at_upper = residual(upper)
    if at_upper > 0.0:
        raise CurveArbitrageError(
            f"{instrument.label}: no discount factor at or below {previous_df!r} solves it. "
            f"The quote implies a discount factor rising into {node}, which is a negative "
            "zero rate over that segment rather than a curve."
        )
    lower = upper
    for _ in range(200):
        lower *= 0.5
        if residual(lower) > 0.0:
            break
    else:  # pragma: no cover - a rate above ~1e60 would be needed to get here
        raise CurveArbitrageError(
            f"{instrument.label}: no admissible discount factor brackets the quote"
        )
    solved = brentq(residual, lower, upper, xtol=_SOLVER_XTOL, rtol=_SOLVER_RTOL, maxiter=200)
    return curve.with_node(node, float(solved))
