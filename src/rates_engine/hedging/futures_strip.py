"""Hedging a swap with a strip of SOFR futures, and what the hedge leaves behind.

The hedge ratio per IMM period is a bucketed delta: bump one contract's quote
by a basis point, rebuild the curve from every quote including the bumped one,
and reprice the swap. That is deliberately *not* the key-rate profile in
:mod:`rates_engine.risk.sensitivities`, which bumps curve nodes instead. The two answer
different questions — how much of the swap's risk this contract can take, and
how the swap's risk is distributed along the curve — and they are reported
under different names for that reason. Each sums to its own parallel, and the
two parallels are different numbers: the key rates sum to the zero-curve DV01,
the bucketed deltas to :attr:`HedgeResult.quote_parallel_dv01`, the DV01 to
every futures quote moving at once. On the README strip that is -21,870.07
against -21,949.91 USD/bp, 0.37% apart, because a basis point on a quoted
ACT/360 forward is not a basis point on the continuously compounded zero
rate (the factor is in :func:`shock_table`). ``test_risk_key_rate.py`` checks
each identity in its own basis and does not compare the two.

**What the shock table is for.** A futures strip pays linearly in the rate and
a swap does not, so a hedge that is exact at the money is not exact anywhere
else. The residual is the swap's convexity, in dollars. Divided by the position
DV01 it is the same thing in basis points, which is the number the CME
whitepaper does not print and the one worth looking at: it says how many basis
points of carry the convexity is worth over the life of the hedge.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

import pandas as pd

from rates_engine.conventions.calendar import HolidayCalendar
from rates_engine.conventions.schedule import next_imm_on_or_after
from rates_engine.core.errors import ConfigurationError, IncompleteStripError
from rates_engine.core.evidence import DataQuality, Degradation, Evidence
from rates_engine.core.money import Currency, require_same_currency
from rates_engine.core.results import EngineResult
from rates_engine.curves.bootstrap import (
    CalibrationInstrument,
    FuturesContract,
    FuturesNode,
    _residual_keys,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.futures import (
    BASIS_POINT,
    SR1_CONTRACT_TENOR,
    SR1_NOTIONAL,
    SR3_CONTRACT_TENOR,
    SR3_NOTIONAL,
    SOFRFuture1M,
)
from rates_engine.pricing.linear import discounted_value
from rates_engine.pricing.projection import Priceable
from rates_engine.risk.bumps import shifted as shift_curves
from rates_engine.risk.sensitivities import dv01

__all__ = [
    "SR1_DV01",
    "SR3_DV01",
    "HedgeResult",
    "ShockTableResult",
    "strip_hedge",
    "shock_table",
    "DEFAULT_SHOCKS_BP",
]

SR3_DV01 = SR3_NOTIONAL * SR3_CONTRACT_TENOR * BASIS_POINT
"""USD 25.00 per basis point, derived from the contract rather than quoted."""

SR1_DV01 = SR1_NOTIONAL * SR1_CONTRACT_TENOR * BASIS_POINT
"""USD 41.67 per basis point: 5,000,000 x 30/360 x 1bp, derived like :data:`SR3_DV01`.

The notional is assumed rather than read from the CME contract spec (see
:mod:`rates_engine.instruments.futures`), and a hedge sized with it records a
degradation that says so.
"""

_CONTRACT_DV01: dict[FuturesContract, float] = {
    FuturesContract.SR1: SR1_DV01,
    FuturesContract.SR3: SR3_DV01,
}
_CONTRACT_DV01_DERIVATION: dict[FuturesContract, str] = {
    FuturesContract.SR1: (
        f"{SR1_NOTIONAL:,.0f} x 30/360 x 1bp = {SR1_DV01:.2f} USD/bp, on a notional "
        "assumed rather than read from the CME contract spec"
    ),
    FuturesContract.SR3: (
        f"{SR3_NOTIONAL:,.0f} x {SR3_CONTRACT_TENOR} x 1bp = {SR3_DV01:.2f} USD/bp"
    ),
}

_TICK_REL_TOL = 1e-3
"""How far a tick may sit from a contract's own and still be that contract's.

A cent of rounding on the published figure, 41.67 for SR1's 41.666...;
anything further is a different number of contracts.
"""

_REFERENCE_DAYS: dict[FuturesContract, tuple[int, ...]] = {
    FuturesContract.SR1: (28, 29, 30, 31),
    FuturesContract.SR3: (84, 91, 98),
}
"""How long each contract's reference period runs, in calendar days.

SR1 references a calendar month. SR3 runs from one IMM date to the next,
and as the third Wednesday falls between the 15th and the 21st of its month
that is 12, 13 or 14 weeks.
"""

_ROLL_DAYS = 3
"""The furthest a business-day roll moves a date on the SIFMA calendar.

A Saturday before a Monday holiday rolls to the Tuesday, and a Friday
holiday to the Monday. A period rolled by one convention moves both its
ends the same way, so its length moves by no more than this, and a period
within this many days of a contract's reference length is that contract's
period with its ends rolled: SR1 25 to 34 days, SR3 81 to 101. The two
ranges are fifty days apart, so no rolled period is ambiguous.
"""

DEFAULT_SHOCKS_BP: tuple[float, ...] = (-100.0, -50.0, -25.0, -10.0, 10.0, 25.0, 50.0, 100.0)
"""Shock grid for :func:`shock_table`, symmetric so convexity shows as asymmetry."""


@dataclass(frozen=True)
class HedgeResult(EngineResult):
    """Contracts per IMM period, and the deltas they were sized from.

    Attributes:
        swap: The instrument being hedged.
        curve_set: The base curves the hedge was sized on.
        contracts: Contract count per futures contract, signed. Positive is
            long. Keyed by the contract's label, or by ``label@period-end``
            where two contracts share a label, so no contract is ever folded
            into another; every dict below uses the same keys.
        bucketed_delta_by_instrument: The swap's DV01 attributable to each
            futures quote, in USD per basis point. ``bump_basis`` for these is
            ``"instrument_quote"``, not a curve node.
        total_contracts: Sum of :attr:`contracts` when every contract is
            worth the same per basis point, ``None`` when the strip mixes SR1
            and SR3: 28.5 SR1 and 299.5 SR3 contracts are not 328 of anything.
            :attr:`total_contracts_by_contract` has each.
        swap_dv01: The swap's parallel DV01 in USD per basis point.
        strip_dv01: The strip's DV01 at these contract counts, each contract
            at its own tick, in USD per basis point.
        contract_dv01: USD per basis point for one contract when every
            contract in the strip is worth the same, ``None`` when the strip
            mixes SR1 and SR3 and there is no one number to report.
        periods: Reference period of each contract, for the report.
        quote_parallel_dv01: The swap's DV01 to a simultaneous one basis point
            move in every futures quote. This, and not the zero-curve parallel
            DV01, is what the bucketed deltas sum to — they are derivatives
            with respect to the quotes, so they add up to the derivative with
            respect to all the quotes at once. The two differ because a basis
            point on an ACT/360 simple forward is not a basis point on a
            continuously compounded zero rate, and because the realised stub
            is not a quote that moves.
        base_forwards: Each contract's forward on the base curve, as a
            decimal on ACT/360; the shock table measures every contract's move
            from it.
        contract_dv01_by_label: USD per basis point for one contract, per
            contract, keyed like :attr:`contracts`. An SR1 is worth USD 41.67
            and an SR3 USD 25.00, and each count was sized at its own.
        total_contracts_by_contract: Sum of :attr:`contracts` per contract
            type, ``"SR1"`` and ``"SR3"``, or ``"caller"`` for contracts sized
            at a ``contract_dv01`` the caller gave for a period that names
            neither. Signed like :attr:`contracts`.
    """

    swap: Priceable
    curve_set: CurveSet
    contracts: dict[str, float]
    bucketed_delta_by_instrument: dict[str, float]
    total_contracts: float | None
    swap_dv01: float
    strip_dv01: float
    contract_dv01: float | None
    periods: dict[str, tuple[date, date]]
    quote_parallel_dv01: float
    base_forwards: dict[str, float]
    contract_dv01_by_label: dict[str, float]
    total_contracts_by_contract: dict[str, float]

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
            "total_contracts_by_contract": dict(self.total_contracts_by_contract),
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
            "contract_dv01_by_label": dict(self.contract_dv01_by_label),
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
    contract_dv01: float | None = None,
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
        contract_dv01: USD per basis point for one contract, applied to every
            contract in the strip. ``None``, the default, sizes each contract
            at its own tick -- SR1 USD 41.67, SR3 USD 25.00 -- naming the
            contract from :attr:`FuturesNode.contract
            <rates_engine.curves.bootstrap.FuturesNode.contract>`, or from
            the period when that is ``None``: a calendar month is an SR1, an
            IMM quarter an SR3, and anything else is sized as an SR3, as every
            contract was before the two were told apart, with a degradation
            saying so. Pass a number only for a contract that is neither: a
            number that contradicts a contract the node declares, or one its
            period names, is refused rather than obeyed, and one applied to
            a period that names no contract keeps that degradation, because
            nothing here can say what such a contract is worth.
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
            contract is never interpolated from its neighbours. A gap at
            either end made only of days the swap's calendar closes is not
            refused: it is the swap's date rolled off a holiday, as when
            Juneteenth falls on the June IMM Wednesday, and the evidence
            lists it under ``coverage_tolerated_gaps``.
        ConfigurationError: Two instruments' keys cannot be told apart even
            with the node date appended; a node declares a contract its
            period contradicts (an SR1 on an IMM quarter, an SR3 on a
            calendar month, or either on a period that is not its own and
            runs more than three days, the longest business-day roll, from
            its reference length: SR1 25 to 34 days, SR3 81 to 101); or
            ``contract_dv01`` contradicts a contract that is declared or
            that the period names.
        ValueError: There are no futures among ``instruments``.
    """
    futures = tuple(i for i in instruments if isinstance(i, FuturesNode))
    if not futures:
        raise ValueError("strip_hedge needs at least one FuturesNode among the instruments")

    default_start, default_end = swap.span
    start = swap_start or default_start
    end = swap_end or default_end
    tolerated_gaps = (
        _check_coverage(futures, start, end, _roll_calendar(swap))
        if require_full_coverage
        else None
    )

    # The bootstrap first: it refuses two instruments pinning one node, which
    # is what a contract passed twice is, under its own name.
    base = bootstrap_discount_curve(as_of, instruments, long_end_source=long_end_source)
    curve_set = CurveSet(base.curve)
    swap_dv01 = dv01(swap, curve_set, source_evidence=(base.evidence,)).value

    # Every dict below is keyed per contract, not per label. Two futures with
    # the same label -- the default "future", or "SR3" on every row of a CLI
    # config -- once shared one entry, so each overwrote the last and the
    # README strip without labels was sized as a single contract: 101.6
    # contracts instead of 878, with no error.
    keys = _contract_keys(instruments, futures)
    sizes, sizing = _contract_sizes(futures, keys, contract_dv01)

    deltas: dict[str, float] = {}
    contracts: dict[str, float] = {}
    periods: dict[str, tuple[date, date]] = {}
    for key, contract in zip(keys, futures, strict=True):
        up = _reprice_with_quote_bump(swap, instruments, contract, +BASIS_POINT, as_of, long_end_source)
        down = _reprice_with_quote_bump(swap, instruments, contract, -BASIS_POINT, as_of, long_end_source)
        delta = (down - up) / 2.0
        deltas[key] = delta
        contracts[key] = -delta / sizes[key]
        periods[key] = (contract.start, contract.end)

    # Summed per tick value, with ``sum`` over each group in contract order,
    # so that a strip of one contract type is its total times its tick to
    # the last bit, as it was when there was only one tick.
    counts_by_size: dict[float, list[float]] = {}
    for key, count in contracts.items():
        counts_by_size.setdefault(sizes[key], []).append(count)
    strip_dv01 = sum(size * sum(counts) for size, counts in counts_by_size.items())
    distinct = set(sizes.values())
    single_size = next(iter(distinct)) if len(distinct) == 1 else None
    # A count of SR1 contracts and a count of SR3 contracts do not add up to
    # a count of anything, so a mixed strip reports one per contract type and
    # no single total, as it reports no single contract_dv01.
    counts_by_contract: dict[str, list[float]] = {}
    for key, count in contracts.items():
        counts_by_contract.setdefault(sizing[key]["contract"] or "caller", []).append(count)
    total_by_contract = {name: sum(counts) for name, counts in counts_by_contract.items()}
    total = sum(contracts.values()) if single_size is not None else None
    quote_parallel_dv01 = _quote_parallel_dv01(
        swap, instruments, futures, as_of, long_end_source
    )
    base_forwards = {
        key: base.curve.forward(contract.start, contract.end)
        for key, contract in zip(keys, futures, strict=True)
    }

    evidence = Evidence(
        produced_by="hedging.strip_hedge",
        fields={
            "instrument": swap.describe(),
            "bump_basis": "instrument_quote",
            "bump_shape": "single_quote",
            "bump_bp": 1.0,
            "difference": "central",
            "contract_dv01": single_size,
            "contract_dv01_derivation": _derivations(sizing, contract_dv01),
            "contract_sizing": sizing,
            "futures_count": len(futures),
            "coverage_checked": require_full_coverage,
            "coverage_tolerated_gaps": tolerated_gaps,
            "swap_span": [start.isoformat(), end.isoformat()],
            "quote_parallel_dv01": quote_parallel_dv01,
            "bucket_sum": sum(deltas.values()),
            "note": (
                "Bucketed delta by instrument quote, not a key-rate profile by curve "
                "node. Each sums to its own parallel: these to quote_parallel_dv01 "
                "(every futures quote moved at once), the key rates to the zero-curve "
                "DV01. The two parallels are different derivatives, about 0.4% apart "
                "at 4%, and answer different questions; see rates_engine.key_rate_dv01."
            ),
        },
        sources=(base.evidence,),
        warnings=_sizing_warnings(futures, keys, sizing, contract_dv01),
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
        contract_dv01=single_size,
        periods=periods,
        quote_parallel_dv01=quote_parallel_dv01,
        base_forwards=base_forwards,
        contract_dv01_by_label=sizes,
        total_contracts_by_contract=total_by_contract,
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
        return discounted_value(swap, CurveSet(curve))

    return (repriced(-BASIS_POINT) - repriced(+BASIS_POINT)) / 2.0


def _contract_keys(
    instruments: tuple[CalibrationInstrument, ...], futures: tuple[FuturesNode, ...]
) -> list[str]:
    """Each contract's key, by the rule the bootstrap keys its residuals with.

    The rule is the bootstrap's own, imported rather than restated: a label
    that appears once among *all* the calibration instruments is its own key,
    and a repeated one gets its node date appended, ``"future@2026-06-17"``.
    Counting repeats among the futures alone keyed a future that shares its
    label with a swap differently from the bootstrap, so the hedge's keys
    could not look up the curve's residuals. The bootstrap has already refused
    two instruments pinning one node, so the suffix separates any two.

    Raises:
        ConfigurationError: A key made this way equals another instrument's,
            e.g. one labelled ``"X@2026-06-17"`` and two labelled ``"X"``.
            Picking one would merge two contracts again.
    """
    keys = _residual_keys(instruments)
    clashes = sorted(key for key, count in Counter(keys).items() if count > 1)
    if clashes:
        raise ConfigurationError(
            f"instrument keys {clashes} still collide after appending the node date to "
            "repeated labels; give these instruments distinct labels"
        )
    by_identity = {id(instrument): key for instrument, key in zip(instruments, keys, strict=True)}
    return [by_identity[id(contract)] for contract in futures]


def _period_contract(contract: FuturesNode) -> FuturesContract | None:
    """The future whose reference period this node spans, or ``None``.

    SR1 references a calendar month and SR3 runs from one IMM date to the
    next, so the two periods never coincide: no IMM date is the first of a
    month. Both are read off the instruments' own definitions rather than
    restated here.
    """
    month = SOFRFuture1M(contract.start)
    if (month.accrual_start, month.accrual_end) == (contract.start, contract.end):
        return FuturesContract.SR1
    following = next_imm_on_or_after(contract.start + timedelta(days=1))
    if contract.start == next_imm_on_or_after(contract.start) and contract.end == following:
        return FuturesContract.SR3
    return None


def _length_contract(contract: FuturesNode) -> FuturesContract | None:
    """The future whose reference period is this node's length, give or take a roll.

    The weaker reading of a period, for one :func:`_period_contract` does not
    recognise: a length within :data:`_ROLL_DAYS` of a contract's reference
    lengths. It cannot say a period *is* that contract's, only that it could
    be -- SR1 months rolled to business days, or an SR3 quarter whose IMM
    date fell on a holiday -- so it checks a declaration and words a hint,
    and never sizes a contract by itself.
    """
    days = (contract.end - contract.start).days
    for name, lengths in _REFERENCE_DAYS.items():
        if min(lengths) - _ROLL_DAYS <= days <= max(lengths) + _ROLL_DAYS:
            return name
    return None


_PERIOD_BASIS = {FuturesContract.SR1: "calendar_month", FuturesContract.SR3: "imm_quarter"}
_PERIOD_SHAPE = {FuturesContract.SR1: "a calendar month", FuturesContract.SR3: "an IMM quarter"}


def _contract_sizing(
    contract: FuturesNode, key: str, contract_dv01: float | None
) -> dict[str, Any]:
    """USD per basis point for one contract, and how it was established.

    Every contract used to be divided by one ``contract_dv01``, SR3's 25.00
    by default, so an SR1 month was bought 1.67 times over. Nothing in the
    result showed it, because the strip DV01 was multiplied back by the same
    tick; the error was in the number of contracts a trader would buy.

    One contract at a time so that the ``hedge`` command can make the same
    checks entry by entry and name the key in the file a refusal comes from.

    Args:
        contract: The futures node.
        key: Its name in the result and in any refusal.
        contract_dv01: The caller's tick in USD per basis point, or ``None``
            for the contract's own.

    Returns:
        The contract (``None`` when the caller's ``contract_dv01`` stands in
        for one the period does not name), the ``basis`` it was read from
        (``declared``, ``calendar_month``, ``imm_quarter``, ``assumed`` for a
        period that is neither and is sized as SR3 as before, or ``caller``
        for such a period sized at the caller's tick), the tick ``dv01`` in
        USD per basis point, and whether it is the ``contract``'s or the
        ``caller``'s.

    Raises:
        ConfigurationError: A declared contract contradicts its period, or
            ``contract_dv01`` contradicts a contract that is declared or that
            the period names.
    """
    from_period = _period_contract(contract)
    name: FuturesContract | None
    if contract.contract is not None:
        declared = FuturesContract(contract.contract)
        if from_period is not None and from_period != declared:
            raise ConfigurationError(
                f"{key} is declared {declared}, but its period {contract.start} to "
                f"{contract.end} is {_PERIOD_SHAPE[from_period]}, which is "
                f"{from_period}'s; fix the contract or the dates"
            )
        # A period that is not exactly the declared contract's is taken on
        # the declaration only if it could be that contract's period rolled
        # to business days. SR1 from 2 February to 4 May 2026 is ninety-one
        # days, an IMM quarter's length: two statements that cannot both be
        # true, and the declaration alone sized it at 59.93 SR1 contracts.
        if from_period is None and _length_contract(contract) is not declared:
            lengths = _REFERENCE_DAYS[declared]
            raise ConfigurationError(
                f"{key} is declared {declared}, but its period {contract.start} to "
                f"{contract.end} runs {(contract.end - contract.start).days} days. "
                f"{declared}'s reference period is {_PERIOD_SHAPE[declared]}, "
                f"{min(lengths)} to {max(lengths)} days, and rolling its ends to business "
                f"days moves that by at most {_ROLL_DAYS} days; fix the contract or the dates"
            )
        name, basis = declared, "declared"
    elif from_period is not None:
        name, basis = from_period, _PERIOD_BASIS[from_period]
    else:
        name, basis = None, "assumed"

    if contract_dv01 is None:
        name = name or FuturesContract.SR3
        size, source = _CONTRACT_DV01[name], "contract"
    elif name is not None:
        # Checked against an inferred contract as well as a declared one:
        # 25.00, the old default, on an undeclared calendar month bought
        # that SR1 1.67 times over without a word. A cent of rounding on
        # the published tick (41.67 for 41.666...) is the same contract;
        # anything further is two statements that cannot both be true.
        if not math.isclose(contract_dv01, _CONTRACT_DV01[name], rel_tol=_TICK_REL_TOL):
            how = "declared" if basis == "declared" else f"by its period, {_PERIOD_SHAPE[name]}"
            raise ConfigurationError(
                f"contract_dv01={contract_dv01} USD/bp contradicts {key}, which is "
                f"{name} ({_CONTRACT_DV01[name]:.2f} USD/bp) {how}; drop contract_dv01 "
                "to size each contract at its own tick"
            )
        size, source = contract_dv01, "caller"
    else:
        basis, size, source = "caller", contract_dv01, "caller"
    return {
        "contract": None if name is None else str(name),
        "basis": basis,
        "dv01": size,
        "dv01_source": source,
    }


def _contract_sizes(
    futures: tuple[FuturesNode, ...], keys: list[str], contract_dv01: float | None
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """:func:`_contract_sizing` for every contract: the tick per key, and how each was read.

    Raises:
        ConfigurationError: As :func:`_contract_sizing`, for the first
            contract that fails.
    """
    sizing = {
        key: _contract_sizing(contract, key, contract_dv01)
        for key, contract in zip(keys, futures, strict=True)
    }
    return {key: entry["dv01"] for key, entry in sizing.items()}, sizing


def _derivations(
    sizing: dict[str, dict[str, Any]], contract_dv01: float | None
) -> dict[str, str]:
    """Where each tick the hedge used came from, one line per contract type."""
    if contract_dv01 is not None:
        # Not "derived from a contract spec", and no longer "not checked
        # against one" either: wherever a contract is declared or its period
        # names one, the caller's number had to match that contract's tick
        # to be accepted at all.
        unchecked = [key for key, entry in sizing.items() if entry["contract"] is None]
        reach = (
            f"unchecked for {', '.join(unchecked)}, whose periods name no contract"
            if unchecked
            else "checked against the tick of every contract in the strip"
        )
        return {
            "caller": (
                f"contract_dv01 = {contract_dv01:.2f} USD/bp, given by the caller and applied "
                "to every contract. It has to match the tick of any contract that is declared "
                f"or that its period names, within {_TICK_REL_TOL:.1%}, so it is {reach}"
            )
        }
    names = dict.fromkeys(FuturesContract(entry["contract"]) for entry in sizing.values())
    return {str(name): _CONTRACT_DV01_DERIVATION[name] for name in names}


def _sizing_warnings(
    futures: tuple[FuturesNode, ...],
    keys: list[str],
    sizing: dict[str, dict[str, Any]],
    contract_dv01: float | None,
) -> tuple[Degradation, ...]:
    """What the sizing assumed, as degradations rather than fields alone.

    Two assumptions. The SR1 notional, on every contract sized at the SR1
    tick, whatever named the contract and whoever gave the tick: it was
    keyed on the engine's classification, so the caller's 41.67 on SR1
    months it could not recognise rested on the same notional and raised no
    flag. And the contract itself, for a period that is neither a calendar
    month nor an IMM quarter, whether it is sized as SR3 by default or at a
    tick the caller gave: the caller branch used to drop the flag, and 25.00
    on SR1 months rolled to business days bought them 1.67 times over and
    read as clean.
    """
    warnings: list[Degradation] = []
    sr1 = [
        key
        for key, entry in sizing.items()
        if math.isclose(entry["dv01"], SR1_DV01, rel_tol=_TICK_REL_TOL)
    ]
    if sr1:
        warnings.append(
            Degradation(
                code="sr1_notional_assumed",
                message=(
                    f"SR1 contracts are sized at {SR1_DV01:.2f} USD per basis point from a "
                    f"{SR1_NOTIONAL:,.0f} USD notional that has not been read from the CME "
                    "contract spec, whoever gave the tick. Affected contracts: "
                    + ", ".join(sr1)
                ),
                data_quality=DataQuality.ASSUMED,
            )
        )
    assumed = [key for key, entry in sizing.items() if entry["basis"] in ("assumed", "caller")]
    if assumed:
        tick = SR3_DV01 if contract_dv01 is None else contract_dv01
        sized = (
            f"so they are sized as SR3 at {SR3_DV01:.2f} USD per basis point"
            if contract_dv01 is None
            else f"so the caller's contract_dv01 of {tick:.2f} USD per basis point is "
            "applied to them unchecked: nothing here says what they are worth"
        )
        warnings.append(
            Degradation(
                code="futures_contract_assumed",
                message=(
                    "Futures " + ", ".join(assumed) + " span periods that are neither a "
                    f"calendar month (SR1) nor one IMM date to the next (SR3), {sized}. "
                    "Declare the contract (FuturesNode.contract, or `contract` on a CLI "
                    "futures entry) to size them at its own tick."
                    + _rolled_period_hints(dict(zip(keys, futures, strict=True)), assumed, tick)
                ),
                data_quality=DataQuality.ASSUMED,
            )
        )
    return tuple(warnings)


def _rolled_period_hints(by_key: dict[str, FuturesNode], assumed: list[str], tick: float) -> str:
    """Which declaration the assumed contracts probably need, by their length.

    Args:
        by_key: The futures nodes by key.
        assumed: The keys whose contract was assumed.
        tick: USD per basis point they were sized at.

    Returns:
        One sentence per contract a group of them is as long as, each with
        the declaration and, where the tick used is not that contract's,
        how far off the count is; empty when none fits.
    """
    hints = []
    for name in FuturesContract:
        fits = [key for key in assumed if _length_contract(by_key[key]) is name]
        if not fits:
            continue
        hint = (
            f" {', '.join(fits)} run as long as {_PERIOD_SHAPE[name]} with its ends rolled "
            f"to business days: if they are {name} contracts, declare contract='{name}'"
        )
        ratio = _CONTRACT_DV01[name] / tick
        if not math.isclose(ratio, 1.0, rel_tol=_TICK_REL_TOL):
            hint += (
                f"; sized at {tick:.2f} USD/bp against an {name}'s "
                f"{_CONTRACT_DV01[name]:.2f}, they are {ratio:.2f} times the contracts an "
                f"{name} hedge needs"
            )
        hints.append(hint + ".")
    return "".join(hints)


def _roll_calendar(swap: Priceable) -> HolidayCalendar | None:
    """The calendar the swap's dates roll on, or ``None`` if it does not say.

    Read rather than assumed: a peso swap rolls on BMV, and a gap is a roll
    only on the calendar that rolled it. An instrument with no
    ``roll_calendar`` gets no tolerance at all, which is the strict reading.
    """
    calendar = getattr(swap, "roll_calendar", None)
    return calendar if isinstance(calendar, HolidayCalendar) else None


def _check_coverage(
    futures: tuple[FuturesNode, ...], start: date, end: date, calendar: HolidayCalendar | None
) -> list[dict[str, str]]:
    """Refuse when the strip leaves a hole anywhere inside the swap's span.

    A gap at either end made only of days ``calendar`` closes is not a hole
    but the swap's own date rolled off a holiday or a weekend. The June 2030
    IMM date is Juneteenth: a swap from the December 2029 IMM date to that
    one ends on Thursday 20 June once its maturity is rolled, the SR3
    quarter on the 19th, and refusing over a day the market is shut blocked
    the textbook IMM-to-IMM hedge. Strictly the day is uncovered, so each
    one tolerated is returned for the evidence. A gap *between* contracts is
    never tolerated: it is not a roll of the swap's dates, and the one day
    nobody holds there is a day nobody holds.

    Args:
        futures: The strip.
        start: First day of the span that must be covered.
        end: End of that span, exclusive.
        calendar: The swap's roll calendar, or ``None`` to tolerate nothing.

    Returns:
        The tolerated gaps, each as ``from``, ``to`` (exclusive) and the
        ``calendar`` that closes every day in it; empty when there are none.

    Raises:
        IncompleteStripError: Some business day of the span is in no
            contract's period, and the message says what to pass instead.
    """
    ordered = sorted(futures, key=lambda f: f.start)
    covered_from = min(f.start for f in ordered)
    covered_to = max(f.end for f in ordered)
    tolerated: list[dict[str, str]] = []
    # What the caller can pass, said in the refusal: the span is a default,
    # and so is the check itself. The CLI exposes the second as a key.
    waive = (
        "or pass require_full_coverage=False (hedge.require_full_coverage: false on the "
        "CLI) to size the partial hedge anyway"
    )
    if start < covered_from:
        opening = _roll_gap(calendar, start, covered_from)
        if opening is None:
            raise IncompleteStripError(
                f"the strip starts at {covered_from} but the swap starts at {start}; "
                "no contract covers the opening period and none is extrapolated. Pass "
                f"swap_start= if the hedge is meant to start later, {waive}"
            )
        tolerated.append(opening)
    if end > covered_to:
        closing = _roll_gap(calendar, covered_to, end)
        if closing is None:
            raise IncompleteStripError(
                f"the strip ends at {covered_to} but the swap runs to {end}; "
                "no contract covers the closing period and none is extrapolated. Pass "
                f"swap_end= if the hedge is meant to stop earlier, {waive}"
            )
        tolerated.append(closing)
    # Walk the contracts in start order, carrying the furthest end reached so
    # far. Comparing each contract only with the one starting next refused
    # valid strips: SR1 months traded under an SR3 quarter overlap it, and
    # the April SR1 ending on 1 May reported a hole to 17 June that the
    # March SR3 quarter, which started earlier, covers to the day.
    reach = ordered[0].end
    for contract in ordered[1:]:
        if contract.start > reach and reach < end and contract.start > start:
            raise IncompleteStripError(
                f"the strip has no contract covering {reach} to {contract.start}, "
                f"which lies inside the swap span {start} to {end}; add the missing "
                f"contract, {waive}"
            )
        reach = max(reach, contract.end)
    return tolerated


def _roll_gap(calendar: HolidayCalendar | None, first: date, last: date) -> dict[str, str] | None:
    """``[first, last)`` as the evidence records a tolerated gap, or ``None`` if it is a hole.

    A gap is tolerated when ``calendar`` closes every day in it, and never
    without a calendar to say so.
    """
    if calendar is None or calendar.business_days(first, last):
        return None
    return {"from": first.isoformat(), "to": last.isoformat(), "calendar": calendar.name}


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
    # Identity, as in _quote_parallel_dv01: two distinct contracts can compare
    # equal. Spelt as id() rather than ``is`` because the protocol-typed
    # instruments and the FuturesNode target look disjoint to mypy --strict,
    # which reports the ``is`` as a non-overlapping identity check.
    target_id = id(target)
    bumped = tuple(
        _with_quote_shift(i, shift) if id(i) == target_id else i for i in instruments
    )
    curve = bootstrap_discount_curve(as_of, bumped, long_end_source=long_end_source).curve
    return discounted_value(swap, CurveSet(curve))


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
                "Every money column is USD. The strip's leg is priced off each "
                f"contract's tick, a dollar constant (SR3 {SR3_DV01:.2f}, SR1 "
                f"{SR1_DV01:.2f} USD/bp), so shock_table refuses a curve set in any "
                "other currency rather than netting the two legs across currencies."
            ),
        }


def shock_table(
    hedge: HedgeResult, shocks_bp: Iterable[float] = DEFAULT_SHOCKS_BP
) -> ShockTableResult:
    """Reprice the hedged position across parallel shocks and report the residual.

    Args:
        hedge: A sized hedge.
        shocks_bp: Parallel shifts in basis points, none of them zero. Any
            iterable; it is read once.

    Returns:
        The :class:`ShockTableResult`. ``net_pnl`` is the swap's convexity in
        dollars; ``net_per_dv01_bp`` is the same thing per basis point of
        position risk.

    Raises:
        CurrencyMismatchError: The hedge's curve set is not in USD. The
            strip's P&L comes from each contract's tick in
            ``contract_dv01_by_label``, a dollar constant, so
            netting it against a peso swap P&L would add two currencies in
            the ``net_pnl`` column and report the sum as one number.
        ConfigurationError: ``shocks_bp`` contains a zero shock.
    """
    # Materialised first: the zero check below used to consume a generator
    # grid, and the rows were then built from nothing.
    grid = tuple(shocks_bp)
    require_same_currency(
        hedge.curve_set.currency,
        Currency.USD,
        operation="netting a futures strip P&L against a swap P&L",
    )
    # Refused rather than dropped or filled with NaN. A zero shock moves
    # nothing, so its row is the hedge itself, and the implied convexity
    # column divides by the shock squared: 0/0, which used to escape as a
    # bare ZeroDivisionError. Dropping it silently would return a table one
    # row shorter than the grid asked for.
    if any(shock == 0.0 for shock in grid):
        raise ConfigurationError(
            f"shocks_bp {list(grid)} contains a 0 bp shock. It moves nothing, and "
            "the implied money convexity is 2 x net / shock^2, undefined at zero; drop "
            "it from the grid -- the unshocked position is the hedge itself"
        )
    base_pv = discounted_value(hedge.swap, hedge.curve_set)
    rows: list[dict[str, float]] = []
    for shock in grid:
        shifted = shift_curves(hedge.curve_set, shock * 1e-4)
        swap_pnl = discounted_value(hedge.swap, shifted) - base_pv
        # A long futures position loses when rates rise, linearly in *its own*
        # forward rate. Using a nominal parallel basis point here instead would
        # leave a first-order residual in the net column and make the swap's
        # convexity unreadable underneath it. One basis point on the
        # continuously compounded ACT/365F zero rate moves an ACT/360 simple
        # forward by (1 + F tau) x 360/365 basis points -- 0.9963 on a 4% IMM
        # quarter -- so the quote parallel DV01 runs about
        # (365/360) / (1 + F tau) times the zero one: 1.00365 on the README
        # strip, not the 365/360 (1.0139) the factor is sometimes rounded to.
        strip_pnl = 0.0
        for label, count in hedge.contracts.items():
            start, end = hedge.periods[label]
            move_bp = (shifted.discount.forward(start, end) - hedge.base_forwards[label]) * 1e4
            strip_pnl -= count * hedge.contract_dv01_by_label[label] * move_bp
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
            "shocks_bp": list(grid),
            "bump_basis": "zero_curve_parallel",
            "base_pv": base_pv,
            "total_contracts": hedge.total_contracts,
            "contract_dv01": hedge.contract_dv01,
            "contract_dv01_by_label": dict(hedge.contract_dv01_by_label),
            "strip_pnl_basis": "each contract's own forward move, not a nominal parallel bp",
            "net_definition": (
                "swap P&L plus strip P&L. Non-zero because the strip pays linearly "
                "in the rate and the swap does not; the residual is the swap's "
                "convexity in dollars, and net_per_dv01_bp is the same in basis points."
            ),
        },
        sources=(hedge.evidence,),
    )
    return ShockTableResult(evidence=evidence, table=table, shocks_bp=grid)
