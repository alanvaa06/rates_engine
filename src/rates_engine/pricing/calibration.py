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
the pricing layer, wraps a real :class:`~rates_engine.instruments.swaps.OISSwap` and computes
its residual
with :func:`~rates_engine.pricing.linear.par_rate_value` -- the very function
that will later price the swap. The curve layer receives it through the
protocol and never learns what it is. A curve bootstrapped from these nodes
reprices its swaps by construction, not by two implementations happening to
match.

``ParSwapNode`` remains for quotes that arrive as a date schedule rather
than a trade -- the CLI's ``par_swaps`` block, the dual-curve tests. It
assumes the floating leg telescopes to ``P(start) - P(end)``, which holds
under this library's schedule convention (accrual ends unadjusted, payments
rolled) only when no payment date rolls; there the two nodes give the same
curve, and ``tests/test_calibration.py`` checks both that and where they part.

**OIS only.** A term-rate swap quote (``IRSwap``) calibrates a projection
curve against a known discount curve -- that is the dual-curve solver's job
(:mod:`rates_engine.curves.dual`) -- and fitting it single-curve here would
silently treat a term quote as an overnight one, so it is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.evidence import Degradation, Provenance
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.swaps import OISSwap
from rates_engine.pricing.collateral import instrument_warnings
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
        label: Name for the evidence record. Defaults to
            ``ois_quote:<maturity>``, unique per swap.
        provenance: Where the quote came from. A ``PROXY`` quality trips the
            bootstrap's ``long_end_source`` gate exactly as for a
            ``ParSwapNode``.

    Raises:
        UnsupportedConventionError: The swap is not an OIS.
        ValueError: The swap has no periods.
    """

    swap: OISSwap
    quoted_rate: float
    label: str = ""
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="file", instrument_kind="ois_par")
    )

    def __post_init__(self) -> None:
        if not isinstance(self.swap, OISSwap):
            raise UnsupportedConventionError(
                f"SwapQuoteNode calibrates a discount curve to OIS quotes; a "
                f"{type(self.swap).__name__} quote is a term rate and calibrates a "
                "projection curve, which is rates_engine.curves.dual's job"
            )
        if not self.swap.fixed_cashflows():
            raise ValueError("the swap has no periods to calibrate to")
        if not self.label:
            # A unique default: two unlabelled nodes must not share a key.
            object.__setattr__(self, "label", f"ois_quote:{self.swap.maturity.isoformat()}")

    @property
    def node_date(self) -> date:
        """The last date the swap's par rate reads a discount factor at.

        The later of the last payment and the last accrual end. Usually the
        payment, which a lag pushes past maturity; but modified following can
        roll a month-end payment *back* before an unadjusted accrual end, and
        the floating leg still reads the discount factor at that end. Pinning
        the earlier date left the curve beyond the node for the next node to
        move, and the fit came undone.
        """
        schedule = self.swap.schedule
        return max(schedule.payment[-1], schedule.accrual_end[-1])

    @property
    def degradations(self) -> tuple[Degradation, ...]:
        """The swap's index assumptions, which the bootstrap carries onto the curve."""
        return instrument_warnings(self.swap)

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
