"""Bootstrapping the same instruments two ways, and reporting where they differ.

PRD-002 AC-5.1. The point of the exercise is that they should *not* differ in
the thing a calibration pins — every instrument reprices under both — and
should differ in the thing no instrument constrains, which is the
instantaneous forward between nodes. Reporting the second while asserting
the first is the honest way to show what an interpolation choice does and
does not buy.

How far the agreement reaches depends on the instruments. Both
interpolations reproduce the discrete forwards exactly, so a strip that reads
discount factors only on nodes -- futures periods, a realised stub -- gives
the two curves the same node factors. A swap paid two days after each period
reads factors inside intervals, where the two interpolations disagree, so
each bootstrap moves its nodes to make the swap reprice and the node factors
differ too (monotone convex gets there by sweeping, see
:mod:`rates_engine.curves.bootstrap`). ``max_df_difference`` reports how much.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from rates_engine.core.evidence import Evidence
from rates_engine.core.money import Currency
from rates_engine.core.results import EngineResult
from rates_engine.curves.bootstrap import CalibrationInstrument, bootstrap_discount_curve
from rates_engine.curves.discount import DiscountCurve

__all__ = ["InterpolationComparison", "compare_interpolations"]


@dataclass(frozen=True)
class InterpolationComparison(EngineResult):
    """Two curves from one set of quotes, and the gap between them.

    Attributes:
        log_linear: The curve under piecewise-constant forwards.
        monotone_convex: The curve under Hagan-West.
        max_residual_bp: Worst instrument repricing residual across both, in
            basis points. Small under both is the claim being tested.
        max_forward_difference_bp: Largest gap between the two instantaneous
            forward curves, in basis points, over the scan.
        max_forward_difference_date: Where that gap occurred.
        max_df_difference: Largest gap in discount factor, which should be
            far smaller than the forward gap — the disagreement is local and
            integrates away.
        sample_dates: The dates scanned.
    """

    log_linear: DiscountCurve
    monotone_convex: DiscountCurve
    max_residual_bp: float
    max_forward_difference_bp: float
    max_forward_difference_date: date
    max_df_difference: float
    sample_dates: tuple[date, ...]

    def payload_fields(self) -> dict[str, Any]:
        """Both curves and the measured differences."""
        return {
            "log_linear": self.log_linear.to_dict(),
            "monotone_convex": self.monotone_convex.to_dict(),
            "max_residual_bp": self.max_residual_bp,
            "max_forward_difference_bp": self.max_forward_difference_bp,
            "max_forward_difference_date": self.max_forward_difference_date.isoformat(),
            "max_df_difference": self.max_df_difference,
            "sampled_points": len(self.sample_dates),
        }


def compare_interpolations(
    as_of: date,
    instruments: tuple[CalibrationInstrument, ...],
    *,
    step_days: int = 7,
    long_end_source: str | None = None,
    currency: Currency = Currency.USD,
) -> InterpolationComparison:
    """Bootstrap one instrument set under both interpolations and measure the gap.

    Args:
        as_of: Valuation date.
        instruments: The calibration set, used unchanged for both.
        step_days: Spacing of the scan across the curve's span.
        long_end_source: Passed through to both bootstraps.
        currency: Passed through to both bootstraps. Both curves must be the
            same one for the comparison to mean anything, which is why it is
            one argument rather than two.

    Returns:
        The :class:`InterpolationComparison`.

    Raises:
        CurveArbitrageError: Either bootstrap produced a non-monotone curve.
        BootstrapResidualError: Either bootstrap missed its tolerance.
    """
    linear = bootstrap_discount_curve(
        as_of,
        instruments,
        interpolation="log_linear_df",
        long_end_source=long_end_source,
        currency=currency,
    )
    convex = bootstrap_discount_curve(
        as_of,
        instruments,
        interpolation="monotone_convex",
        long_end_source=long_end_source,
        currency=currency,
    )
    residual = max(
        max((abs(r) for r in linear.residuals_bp.values()), default=0.0),
        max((abs(r) for r in convex.residuals_bp.values()), default=0.0),
    )

    last = linear.curve.nodes[-1]
    scan: list[date] = []
    day = as_of + timedelta(days=step_days)
    while day <= last:
        scan.append(day)
        day += timedelta(days=step_days)

    worst_forward, worst_date, worst_df = 0.0, scan[0] if scan else last, 0.0
    for sample in scan:
        gap = abs(
            convex.curve.instantaneous_forward(sample)
            - linear.curve.instantaneous_forward(sample)
        )
        if gap > worst_forward:
            worst_forward, worst_date = gap, sample
        worst_df = max(worst_df, abs(convex.curve.df(sample) - linear.curve.df(sample)))

    evidence = Evidence(
        produced_by="curves.compare_interpolations",
        fields={
            "interpolations": ["log_linear_df", "monotone_convex"],
            "instruments": len(instruments),
            "max_residual_bp": residual,
            "max_forward_difference_bp": worst_forward * 1e4,
            "max_forward_difference_date": worst_date.isoformat(),
            "max_df_difference": worst_df,
            "scan_step_days": step_days,
            "note": (
                "Both bootstraps reprice every instrument; the monotone convex one is "
                "swept until it does. Where every instrument reads discount factors on "
                "nodes only, the node factors agree and the curves differ in the "
                "instantaneous forward between nodes, which no instrument constrains. "
                "Where an instrument reads a factor inside an interval (a payment lag, "
                "an intermediate coupon) the node factors differ as well, by up to "
                "max_df_difference."
            ),
        },
        sources=(linear.evidence, convex.evidence),
    )
    return InterpolationComparison(
        evidence=evidence,
        log_linear=linear.curve,
        monotone_convex=convex.curve,
        max_residual_bp=residual,
        max_forward_difference_bp=worst_forward * 1e4,
        max_forward_difference_date=worst_date,
        max_df_difference=worst_df,
        sample_dates=tuple(scan),
    )
