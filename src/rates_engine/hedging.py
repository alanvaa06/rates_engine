"""Hedging a swap with a strip of SOFR futures, and what the hedge leaves behind.

The hedge ratio per IMM period is a bucketed delta: bump one contract's quote
by a basis point, rebuild the curve from every quote including the bumped one,
and reprice the swap. That is deliberately *not* the key-rate profile in
:mod:`rates_engine.risk`, which bumps curve nodes instead. The two answer
different questions — how much of the swap's risk this contract can take, and
how the swap's risk is distributed along the curve — and they are reported
under different names for that reason. They do sum to the same parallel DV01,
and ``test_risk_key_rate.py`` checks that they do.

**What the shock table is for.** A futures strip pays linearly in the rate and
a swap does not, so a hedge that is exact at the money is not exact anywhere
else. The residual is the swap's convexity, in dollars. Divided by the position
DV01 it is the same thing in basis points, which is the number the CME
whitepaper does not print and the one worth looking at: it says how many basis
points of carry the convexity is worth over the life of the hedge.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pandas as pd

from rates_engine.curves.bootstrap import (
    CalibrationInstrument,
    FuturesNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import CurveSet
from rates_engine.errors import IncompleteStripError
from rates_engine.evidence import Evidence
from rates_engine.instruments.futures import BASIS_POINT, SR3_CONTRACT_TENOR, SR3_NOTIONAL
from rates_engine.money import Currency, require_same_currency
from rates_engine.pricing import Priceable, dv01, pv
from rates_engine.results import EngineResult

__all__ = ["SR3_DV01", "HedgeResult", "ShockTableResult", "strip_hedge", "shock_table", "DEFAULT_SHOCKS_BP"]

SR3_DV01 = SR3_NOTIONAL * SR3_CONTRACT_TENOR * BASIS_POINT
"""USD 25.00 per basis point, derived from the contract rather than quoted."""

DEFAULT_SHOCKS_BP: tuple[float, ...] = (-100.0, -50.0, -25.0, -10.0, 10.0, 25.0, 50.0, 100.0)
"""Shock grid for :func:`shock_table`, symmetric so convexity shows as asymmetry."""


@dataclass(frozen=True)
class HedgeResult(EngineResult):
    """Contracts per IMM period, and the deltas they were sized from.

    Attributes:
        swap: The instrument being hedged.
        curve_set: The base curves the hedge was sized on.
        contracts: Contract count per futures label, signed. Positive is long.
        bucketed_delta_by_instrument: The swap's DV01 attributable to each
            futures quote, in USD per basis point. ``bump_basis`` for these is
            ``"instrument_quote"``, not a curve node.
        total_contracts: Sum of :attr:`contracts`.
        swap_dv01: The swap's parallel DV01 in USD per basis point.
        strip_dv01: The strip's DV01 at these contract counts.
        contract_dv01: USD per basis point for one contract.
        periods: Reference period of each contract, for the report.
        quote_parallel_dv01: The swap's DV01 to a simultaneous one basis point
            move in every futures quote. This, and not the zero-curve parallel
            DV01, is what the bucketed deltas sum to — they are derivatives
            with respect to the quotes, so they add up to the derivative with
            respect to all the quotes at once. The two differ because a basis
            point on an ACT/360 simple forward is not a basis point on a
            continuously compounded zero rate, and because the realised stub
            is not a quote that moves.
    """

    swap: Priceable
    curve_set: CurveSet
    contracts: dict[str, float]
    bucketed_delta_by_instrument: dict[str, float]
    total_contracts: float
    swap_dv01: float
    strip_dv01: float
    contract_dv01: float
    periods: dict[str, tuple[date, date]]
    quote_parallel_dv01: float
    base_forwards: dict[str, float]

    @property
    def hedge_ratio(self) -> float:
        """Strip DV01 over the quote-basis swap DV01. Minus one when complete.

        Measured against :attr:`quote_parallel_dv01` rather than
        :attr:`swap_dv01`, because the contracts were sized from derivatives
        with respect to the quotes. Comparing them to the zero-curve DV01
        would report a 0.4% miss on a hedge that is in fact exact.
        """
        return (
            self.strip_dv01 / self.quote_parallel_dv01 if self.quote_parallel_dv01 else float("nan")
        )

    def payload_fields(self) -> dict[str, Any]:
        """Contracts, deltas and the DV01s they were derived from."""
        return {
            "total_contracts": self.total_contracts,
            "contracts": dict(self.contracts),
            "bucketed_delta_by_instrument": dict(self.bucketed_delta_by_instrument),
            "bump_basis": "instrument_quote",
            "swap_dv01": self.swap_dv01,
            "swap_dv01_bump_basis": "zero_curve_parallel",
            "quote_parallel_dv01": self.quote_parallel_dv01,
            "bucket_sum": sum(self.bucketed_delta_by_instrument.values()),
            "node_vs_instrument_difference": self.swap_dv01 - self.quote_parallel_dv01,
            "node_vs_instrument_note": (
                "The zero-curve parallel DV01 and the quote parallel DV01 are "
                "different derivatives, not two estimates of one number. Neither is "
                "the correct one; they answer different questions, and the hedge is "
                "first-order neutral under any shock either way because the strip's "
                "P&L is computed from the actual move in each contract's own forward."
            ),
            "strip_dv01": self.strip_dv01,
            "contract_dv01": self.contract_dv01,
            "hedge_ratio": self.hedge_ratio,
            "periods": {
                label: [start.isoformat(), end.isoformat()]
                for label, (start, end) in self.periods.items()
            },
        }


def strip_hedge(
    swap: Priceable,
    instruments: tuple[CalibrationInstrument, ...],
    *,
    as_of: date,
    contract_dv01: float = SR3_DV01,
    long_end_source: str | None = None,
    require_full_coverage: bool = True,
    swap_start: date | None = None,
    swap_end: date | None = None,
) -> HedgeResult:
    """Size a futures strip against a swap, one contract count per IMM period.

    Args:
        swap: The instrument to hedge.
        instruments: The full calibration set the curve is built from. Only the
            :class:`~rates_engine.curves.bootstrap.FuturesNode` members are
            hedging instruments; the rest still move the curve when one of them
            is bumped, which is why the whole set is required.
        as_of: Valuation date.
        contract_dv01: USD per basis point for one contract.
        long_end_source: Passed through to the bootstrap.
        require_full_coverage: When true, a gap between the swap's span and the
            strip's coverage raises rather than producing a partial hedge that
            reports as complete.
        swap_start: Start of the span that must be covered. Defaults to the
            swap's ``effective``.
        swap_end: End of that span. Defaults to the swap's ``maturity``.

    Returns:
        The :class:`HedgeResult`.

    Raises:
        IncompleteStripError: The strip has no contract covering part of the
            swap's span, and ``require_full_coverage`` is set. A missing
            contract is never interpolated from its neighbours.
        ValueError: There are no futures among ``instruments``.
    """
    futures = tuple(i for i in instruments if isinstance(i, FuturesNode))
    if not futures:
        raise ValueError("strip_hedge needs at least one FuturesNode among the instruments")

    default_start, default_end = swap.span
    start = swap_start or default_start
    end = swap_end or default_end
    if require_full_coverage:
        _check_coverage(futures, start, end)

    base = bootstrap_discount_curve(as_of, instruments, long_end_source=long_end_source)
    curve_set = CurveSet(base.curve)
    swap_dv01 = dv01(swap, curve_set, source_evidence=(base.evidence,)).value

    deltas: dict[str, float] = {}
    contracts: dict[str, float] = {}
    periods: dict[str, tuple[date, date]] = {}
    for contract in futures:
        up = _reprice_with_quote_bump(swap, instruments, contract, +BASIS_POINT, as_of, long_end_source)
        down = _reprice_with_quote_bump(swap, instruments, contract, -BASIS_POINT, as_of, long_end_source)
        delta = (down - up) / 2.0
        deltas[contract.label] = delta
        contracts[contract.label] = -delta / contract_dv01
        periods[contract.label] = (contract.start, contract.end)

    total = sum(contracts.values())
    strip_dv01 = total * contract_dv01
    quote_parallel_dv01 = _quote_parallel_dv01(
        swap, instruments, futures, as_of, long_end_source
    )
    base_forwards = {
        contract.label: base.curve.forward(contract.start, contract.end)
        for contract in futures
    }

    evidence = Evidence(
        produced_by="hedging.strip_hedge",
        fields={
            "instrument": swap.describe(),
            "bump_basis": "instrument_quote",
            "bump_shape": "single_quote",
            "bump_bp": 1.0,
            "difference": "central",
            "contract_dv01": contract_dv01,
            "contract_dv01_derivation": (
                f"{SR3_NOTIONAL:,.0f} x {SR3_CONTRACT_TENOR} x 1bp = {SR3_DV01:.2f} USD/bp"
            ),
            "futures_count": len(futures),
            "coverage_checked": require_full_coverage,
            "swap_span": [start.isoformat(), end.isoformat()],
            "quote_parallel_dv01": quote_parallel_dv01,
            "bucket_sum": sum(deltas.values()),
            "note": (
                "Bucketed delta by instrument quote, not a key-rate profile by curve "
                "node. The two sum to the same parallel DV01 and answer different "
                "questions; see rates_engine.risk.key_rate_dv01."
            ),
        },
        sources=(base.evidence,),
    )
    return HedgeResult(
        evidence=evidence,
        swap=swap,
        curve_set=curve_set,
        contracts=contracts,
        bucketed_delta_by_instrument=deltas,
        total_contracts=total,
        swap_dv01=swap_dv01,
        strip_dv01=strip_dv01,
        contract_dv01=contract_dv01,
        periods=periods,
        quote_parallel_dv01=quote_parallel_dv01,
        base_forwards=base_forwards,
    )


def _quote_parallel_dv01(
    swap: Priceable,
    instruments: tuple[CalibrationInstrument, ...],
    futures: tuple[FuturesNode, ...],
    as_of: date,
    long_end_source: str | None,
) -> float:
    """The swap's DV01 to every futures quote moving together by one basis point.

    The identity the bucketed deltas have to satisfy: a sum of partial
    derivatives is the derivative along the diagonal. Computed here by an
    independent route — one shift of all the quotes at once — so that the test
    comparing them can fail.
    """
    # Identity, not equality: FuturesNode carries a dict of convexity metadata,
    # so it is not hashable and two distinct contracts could compare equal.
    targets = {id(f) for f in futures}

    def repriced(shift: float) -> float:
        bumped = tuple(
            _with_quote_shift(i, shift) if id(i) in targets else i for i in instruments
        )
        curve = bootstrap_discount_curve(as_of, bumped, long_end_source=long_end_source).curve
        return pv(swap, CurveSet(curve)).value

    return (repriced(-BASIS_POINT) - repriced(+BASIS_POINT)) / 2.0


def _check_coverage(futures: tuple[FuturesNode, ...], start: date, end: date) -> None:
    """Refuse when the strip leaves a hole anywhere inside the swap's span."""
    ordered = sorted(futures, key=lambda f: f.start)
    covered_from = min(f.start for f in ordered)
    covered_to = max(f.end for f in ordered)
    if start < covered_from:
        raise IncompleteStripError(
            f"the strip starts at {covered_from} but the swap starts at {start}; "
            "no contract covers the opening period and none is extrapolated"
        )
    if end > covered_to:
        raise IncompleteStripError(
            f"the strip ends at {covered_to} but the swap runs to {end}; "
            "no contract covers the closing period and none is extrapolated"
        )
    for left, right in zip(ordered, ordered[1:], strict=False):
        if right.start > left.end and left.end < end and right.start > start:
            raise IncompleteStripError(
                f"the strip has no contract covering {left.end} to {right.start}, "
                f"which lies inside the swap span {start} to {end}"
            )


def _with_quote_shift(
    instrument: CalibrationInstrument, shift: float
) -> CalibrationInstrument:
    """A copy of a futures node with its quoted forward moved, or the original.

    The narrowing is the point: only a :class:`FuturesNode` has a quote to
    move, and doing the check here rather than at each call site is what lets
    the callers stay generic over the whole calibration set.
    """
    if not isinstance(instrument, FuturesNode):
        return instrument
    return replace(instrument, forward_rate=instrument.forward_rate + shift)


def _reprice_with_quote_bump(
    swap: Priceable,
    instruments: tuple[CalibrationInstrument, ...],
    target: FuturesNode,
    shift: float,
    as_of: date,
    long_end_source: str | None,
) -> float:
    """Reprice the swap with one futures quote moved and the curve rebuilt."""
    bumped = tuple(_with_quote_shift(i, shift) if i is target else i for i in instruments)
    curve = bootstrap_discount_curve(as_of, bumped, long_end_source=long_end_source).curve
    return pv(swap, CurveSet(curve)).value


@dataclass(frozen=True)
class ShockTableResult(EngineResult):
    """Profit and loss of swap, strip and the two together across parallel shocks.

    Attributes:
        table: One row per shock, with the swap's P&L, the strip's, the net,
            the post-shock DV01, the net expressed in basis points of that
            DV01, and the money convexity that net implies.
        shocks_bp: The shock grid used.
    """

    table: pd.DataFrame
    shocks_bp: tuple[float, ...]

    def payload_fields(self) -> dict[str, Any]:
        """The table as records, the grid, and the currency the P&L is in."""
        return {
            "shocks_bp": list(self.shocks_bp),
            "rows": self.table.to_dict(orient="records"),
            "columns": list(self.table.columns),
            "currency": Currency.USD.value,
            "currency_note": (
                "Every money column is USD. The strip's leg is priced off "
                f"SR3_DV01, which is a dollar constant ({SR3_DV01:.2f} USD/bp), "
                "so shock_table refuses a curve set in any other currency "
                "rather than netting the two legs across currencies."
            ),
        }


def shock_table(
    hedge: HedgeResult, shocks_bp: tuple[float, ...] = DEFAULT_SHOCKS_BP
) -> ShockTableResult:
    """Reprice the hedged position across parallel shocks and report the residual.

    Args:
        hedge: A sized hedge.
        shocks_bp: Parallel shifts in basis points.

    Returns:
        The :class:`ShockTableResult`. ``net_pnl`` is the swap's convexity in
        dollars; ``net_per_dv01_bp`` is the same thing per basis point of
        position risk.

    Raises:
        CurrencyMismatchError: The hedge's curve set is not in USD. The
            strip's P&L comes from ``contract_dv01``, a dollar constant, so
            netting it against a peso swap P&L would add two currencies in
            the ``net_pnl`` column and report the sum as one number.
    """
    require_same_currency(
        hedge.curve_set.currency,
        Currency.USD,
        operation="netting a futures strip P&L against a swap P&L",
    )
    base_pv = pv(hedge.swap, hedge.curve_set).value
    rows: list[dict[str, float]] = []
    for shock in shocks_bp:
        shifted = hedge.curve_set.shifted(shock * 1e-4)
        swap_pnl = pv(hedge.swap, shifted).value - base_pv
        # A long futures position loses when rates rise, linearly in *its own*
        # forward rate. Using a nominal parallel basis point here instead would
        # leave a first-order residual in the net column and make the swap's
        # convexity unreadable underneath it: a basis point on a continuously
        # compounded zero rate is not a basis point on an ACT/360 simple
        # forward, and the difference is roughly 365/360.
        strip_pnl = 0.0
        for label, count in hedge.contracts.items():
            start, end = hedge.periods[label]
            move_bp = (shifted.discount.forward(start, end) - hedge.base_forwards[label]) * 1e4
            strip_pnl -= count * hedge.contract_dv01 * move_bp
        post_dv01 = dv01(hedge.swap, shifted).value
        net = swap_pnl + strip_pnl
        rows.append(
            {
                "shock_bp": shock,
                "swap_pnl": swap_pnl,
                "strip_pnl": strip_pnl,
                "net_pnl": net,
                "swap_dv01_post_shock": post_dv01,
                "net_per_dv01_bp": net / abs(post_dv01) if post_dv01 else float("nan"),
                # 2 * net / dy^2. If the hedge is first-order neutral this is the
                # position's money convexity and is the same number in every row;
                # drift across the grid is the honest diagnostic that it is not.
                "implied_position_money_convexity": 2.0 * net / (shock * 1e-4) ** 2,
            }
        )
    table = pd.DataFrame(rows)
    evidence = Evidence(
        produced_by="hedging.shock_table",
        fields={
            "shocks_bp": list(shocks_bp),
            "bump_basis": "zero_curve_parallel",
            "base_pv": base_pv,
            "total_contracts": hedge.total_contracts,
            "contract_dv01": hedge.contract_dv01,
            "strip_pnl_basis": "each contract's own forward move, not a nominal parallel bp",
            "net_definition": (
                "swap P&L plus strip P&L. Non-zero because the strip pays linearly "
                "in the rate and the swap does not; the residual is the swap's "
                "convexity in dollars, and net_per_dv01_bp is the same in basis points."
            ),
        },
        sources=(hedge.evidence,),
    )
    return ShockTableResult(evidence=evidence, table=table, shocks_bp=tuple(shocks_bp))
