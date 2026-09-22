"""Calibration nodes built from instruments: a curve calibrated to the swap it will price.

A bootstrap needs, from each quote, one node date and a residual. The curve
layer defines that contract --
:class:`~rates_engine.curves.bootstrap.CalibrationInstrument` -- and does not
import a single product, so a curve can be calibrated without the instrument
layer. That rule stays. What it used to cost was a second copy of the swap
mathematics: :class:`~rates_engine.curves.bootstrap.ParSwapNode` carries dates
and year fractions and its own annuity and par-rate formulas, and nothing
guaranteed they agreed with the pricer's.

:class:`SwapQuoteNode` closes that gap from the other side. It lives here, in
the pricing layer, wraps a real :class:`~rates_engine.instruments.swaps.OISSwap`
or :class:`~rates_engine.instruments.swaps.IRSwap` and computes its residual
with :func:`~rates_engine.pricing.linear.par_rate_value` -- the very function
that will later price the swap. The curve layer receives it through the
protocol and never learns what it is. A curve bootstrapped from these nodes
reprices its swaps by construction, not by two implementations happening to
match.

``ParSwapNode`` remains for quotes that arrive as a date schedule rather
than a trade -- the CLI's ``par_swaps`` block, the dual-curve tests -- and
for a lag-free OIS the two agree, which ``tests/test_calibration.py`` checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rates_engine.core.evidence import Provenance
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.swaps import IRSwap, OISSwap
from rates_engine.pricing.linear import par_rate_value

__all__ = ["SwapQuoteNode"]


@dataclass(frozen=True)
class SwapQuoteNode:
    """A par quote on a real swap, pinning the curve at the swap's last payment.

    Satisfies :class:`~rates_engine.curves.bootstrap.CalibrationInstrument`,
    so :func:`~rates_engine.curves.bootstrap.bootstrap_discount_curve` accepts
    it beside futures and stub nodes.

    Attributes:
        swap: The quoted swap. Its ``fixed_rate`` is ignored: the quote is the
            par rate the curve must reproduce.
        quoted_rate: The par rate as a decimal.
        label: Name for the evidence record.
        provenance: Where the quote came from. A ``PROXY`` quality trips the
            bootstrap's ``long_end_source`` gate exactly as for a
            ``ParSwapNode``.

    Raises:
        ValueError: The swap has no periods.
    """

    swap: OISSwap | IRSwap
    quoted_rate: float
    label: str = "swap_quote"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="file", instrument_kind="ois_par")
    )

    def __post_init__(self) -> None:
        if not self.swap.fixed_cashflows():
            raise ValueError(f"{self.label}: the swap has no periods to calibrate to")

    @property
    def node_date(self) -> date:
        """The swap's last payment date, fixed or floating, which is the last
        discount factor its par rate reads."""
        payments = [self.swap.fixed_cashflows()[-1].payment_date]
        if isinstance(self.swap, IRSwap):
            payments.append(self.swap.float_schedule.payment[-1])
        else:
            payments.append(self.swap.schedule.payment[-1])
        return max(payments)

    def residual_bp(self, curve: DiscountCurve) -> float:
        """Model par rate minus the quote, in basis points, on a single-curve set.

        Args:
            curve: The trial curve; it both projects and discounts.

        Returns:
            The signed residual in basis points.
        """
        return (par_rate_value(self.swap, CurveSet(curve)) - self.quoted_rate) * 1e4

    def describe(self) -> dict[str, Any]:
        """Terms of the node, including the swap it was built from."""
        return {
            "kind": "swap_quote",
            "quoted_rate": self.quoted_rate,
            "maturity": self.node_date.isoformat(),
            "swap": self.swap.describe(),
        }
