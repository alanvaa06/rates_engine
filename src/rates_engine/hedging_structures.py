"""Seven ways to hedge a transaction exposure, priced and laid side by side.

The CFA Level III framing, implemented as arithmetic. A treasurer with a
foreign-currency payable or receivable can leave it open, sell it forward,
buy an option, or build one of four option combinations, and the question
is never which is best — it is what each one costs and what it gives up.

**This module does not recommend.** It returns a table. Cost goes up,
protection goes up, and participation in a favourable move goes down; that
trade-off is reported as labelled fields rather than as a sentence, and
there is no function here whose name contains ``recommend`` because there
is no such function. PRD-003 AC-4.5 asks for a test of that, and
``tests/test_hedge_structures.py`` scans the public surface for it.

**Which option protects depends on the direction.** A treasurer whose
functional currency is the quote currency and who *owes* base currency is
hurt when the pair rises, so protection is a call. One who is *owed* base
currency is hurt when it falls, so protection is a put. Getting this
backwards produces a table of structures that all make the exposure worse,
which is why :class:`Exposure` carries the direction and the structures ask
it rather than assuming a receivable.

**The unhedged row is deliberate.** A comparison whose cheapest option is
still an option hides the fact that doing nothing is free and sometimes
right. It is the zero-cost, zero-protection, full-participation baseline
every other row is read against.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from rates_engine.errors import ImplausibleInputError
from rates_engine.evidence import Evidence
from rates_engine.fx import garman_kohlhagen as gk
from rates_engine.fx.quote import CurrencyPair
from rates_engine.results import EngineResult
from rates_engine.volatility.kinds import OptionKind

__all__ = [
    "ExposureDirection",
    "Exposure",
    "StructureQuote",
    "StructureResult",
    "StructureComparison",
    "TRADE_OFF_FRAME",
    "compare_structures",
    "zero_cost_collar_strike",
]

TRADE_OFF_FRAME: dict[str, str] = {
    "upfront_cost": "higher",
    "protection": "better",
    "upside_participation": "worse",
    "statement": (
        "Across these structures, paying more upfront buys a better worst case "
        "and gives up more of a favourable move. Which point on that line is "
        "right depends on the treasury policy, not on the numbers."
    ),
}
"""The trade-off, as labelled fields rather than as advice.

AC-4.4. The direction of each axis is named so a caller can sort on it; the
sentence explains the axes and stops short of choosing between them, which
is the line this module does not cross.
"""


class ExposureDirection(StrEnum):
    """Which way the exposure hurts.

    ``PAYABLE``
        Base currency is owed. A rise in the pair costs money, so protection
        is a call.
    ``RECEIVABLE``
        Base currency is expected. A fall in the pair costs money, so
        protection is a put.
    """

    PAYABLE = "payable"
    RECEIVABLE = "receivable"

    @property
    def protective_kind(self) -> OptionKind:
        """The option that limits the loss."""
        return OptionKind.CALL if self is ExposureDirection.PAYABLE else OptionKind.PUT

    @property
    def sign(self) -> float:
        """``-1`` for a payable, ``+1`` for a receivable.

        The sign of the unhedged position's sensitivity to the pair: a
        receivable gains when the pair rises, a payable loses.
        """
        return -1.0 if self is ExposureDirection.PAYABLE else 1.0


@dataclass(frozen=True)
class Exposure:
    """What is being hedged.

    Attributes:
        amount: Size in base currency, positive. The direction is carried
            separately rather than as a sign, because a negative amount and
            a payable are two ways to say the same thing and supporting both
            invites saying it twice.
        direction: Payable or receivable.
        settlement: When it settles.
        pair: The currency pair, base first.
    """

    amount: float
    direction: ExposureDirection
    settlement: date
    pair: CurrencyPair

    def __post_init__(self) -> None:
        if self.amount <= 0.0:
            raise ValueError(
                f"an exposure's amount is a positive size in {self.pair.base.value}; "
                f"its direction says which way it hurts. Got {self.amount!r}."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the exposure and the pair it is in."""
        return {
            "amount": self.amount,
            "direction": self.direction.value,
            "settlement": self.settlement.isoformat(),
            **self.pair.to_dict(),
        }


@dataclass(frozen=True)
class StructureQuote:
    """The market a comparison is struck on.

    Attributes:
        spot: Spot rate.
        expiry: Time to settlement in years.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        volatility: A single lognormal volatility, or a callable taking a
            strike and returning one — pass a
            :class:`~rates_engine.fx.vannavolga.VannaVolgaSmile`'s reader to
            price every leg on its own point of the smile, which is the
            honest way to price a collar.
    """

    spot: float
    expiry: float
    r_domestic: float
    r_foreign: float
    volatility: float | Callable[[float], float]

    def vol_at(self, strike: float) -> float:
        """The volatility for a leg struck at ``strike``."""
        if callable(self.volatility):
            return float(self.volatility(strike))
        return float(self.volatility)

    @property
    def forward(self) -> float:
        """The outright forward these rates imply."""
        return gk.forward(self.spot, self.expiry, self.r_domestic, self.r_foreign)

    def price(self, strike: float, kind: OptionKind) -> float:
        """One option, per unit of base currency."""
        return gk.price(
            self.spot, strike, self.expiry, self.r_domestic, self.r_foreign,
            self.vol_at(strike), kind,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the market, saying whether a smile was used."""
        return {
            "spot": self.spot,
            "forward": self.forward,
            "expiry": self.expiry,
            "r_domestic": self.r_domestic,
            "r_foreign": self.r_foreign,
            "volatility_source": "smile" if callable(self.volatility) else "single",
            "volatility": None if callable(self.volatility) else self.volatility,
        }


@dataclass(frozen=True)
class _Leg:
    """One bought or sold option inside a structure."""

    kind: OptionKind
    strike: float
    quantity: float  # +1 bought, -1 sold

    def payoff(self, spot: float) -> float:
        return self.quantity * max(self.kind.sign * (spot - self.strike), 0.0)


@dataclass(frozen=True)
class StructureResult(EngineResult):
    """One structure, costed and characterised.

    Attributes:
        name: The structure's name.
        upfront_cost: Net premium in quote currency, per unit of base.
            Positive is paid, negative is received.
        total_cost: The same, scaled by the exposure.
        worst_case_rate: The least favourable effective rate achievable, in
            quote per base. ``None`` when the structure leaves the exposure
            unbounded, which is information rather than a gap.
        best_case_rate: The most favourable, or ``None`` when unbounded.
        upside_participation: Fraction of a favourable move retained, in
            ``[0, 1]``, measured over the reported spot grid.
        hedge_ratio: The fraction of the exposure hedged.
        legs: ``(kind, strike, quantity)`` per option leg.
        forward_amount: Base currency sold or bought forward, signed.
        payoff_grid: Effective rate at each spot on the grid.
        spot_grid: The spots it was evaluated at.
    """

    name: str
    upfront_cost: float
    total_cost: float
    worst_case_rate: float | None
    best_case_rate: float | None
    upside_participation: float
    hedge_ratio: float
    legs: tuple[tuple[str, float, float], ...]
    forward_amount: float
    payoff_grid: tuple[float, ...]
    spot_grid: tuple[float, ...]

    def payload_fields(self) -> dict[str, Any]:
        """Numbers and names. No sentence anywhere says which to choose."""
        return {
            "name": self.name,
            "upfront_cost": self.upfront_cost,
            "total_cost": self.total_cost,
            "worst_case_rate": self.worst_case_rate,
            "best_case_rate": self.best_case_rate,
            "upside_participation": self.upside_participation,
            "hedge_ratio": self.hedge_ratio,
            "legs": [
                {"kind": kind, "strike": strike, "quantity": quantity}
                for kind, strike, quantity in self.legs
            ],
            "forward_amount": self.forward_amount,
            "payoff_grid": list(self.payoff_grid),
            "spot_grid": list(self.spot_grid),
        }


def zero_cost_collar_strike(
    quote: StructureQuote,
    protective_strike: float,
    protective_kind: OptionKind,
    *,
    tolerance: float = 1e-12,
) -> float:
    """The strike at which the sold wing exactly pays for the bought one.

    Args:
        quote: The market.
        protective_strike: Strike of the option being bought.
        protective_kind: Which kind that is.
        tolerance: Bisection tolerance on the strike.

    Returns:
        The strike of the opposite-kind option to sell.

    Raises:
        ImplausibleInputError: No strike pays for it, which happens when the
            protection bought is worth more than the entire opposite wing.
    """
    target = quote.price(protective_strike, protective_kind)
    sold_kind = protective_kind.opposite
    # A sold put pays more the higher its strike; a sold call pays more the
    # lower its strike. Bracket accordingly and bisect.
    low, high = quote.spot * 1e-3, quote.spot * 10.0
    def sold(strike: float) -> float:
        return quote.price(strike, sold_kind)

    if sold_kind is OptionKind.PUT:
        if sold(high) < target:
            raise ImplausibleInputError(
                f"no put strike up to {high:.4f} raises the {target:.6f} needed to pay "
                f"for the {protective_kind.value} at {protective_strike:.4f}. The "
                "protection asked for is worth more than the whole opposite wing, so "
                "there is no zero-cost collar here — widen the protective strike."
            )
        for _ in range(400):
            mid = 0.5 * (low + high)
            if sold(mid) < target:
                low = mid
            else:
                high = mid
            if high - low < tolerance:
                break
        return 0.5 * (low + high)
    if sold(low) < target:
        raise ImplausibleInputError(
            f"no call strike down to {low:.4f} raises the {target:.6f} needed to pay "
            f"for the {protective_kind.value} at {protective_strike:.4f}. There is no "
            "zero-cost collar here — widen the protective strike."
        )
    for _ in range(400):
        mid = 0.5 * (low + high)
        if sold(mid) < target:
            high = mid
        else:
            low = mid
        if high - low < tolerance:
            break
    return 0.5 * (low + high)


def _effective_rate(
    exposure: Exposure,
    quote: StructureQuote,
    legs: tuple[_Leg, ...],
    forward_amount: float,
    forward_rate: float,
    upfront: float,
    spot: float,
) -> float:
    """The rate the treasurer ends up transacting at, all in.

    Everything is expressed per unit of base currency and in quote currency,
    so the number is directly comparable across structures: for a payable it
    is what each unit of base currency ends up costing, and lower is better;
    for a receivable it is what each unit fetches, and higher is better.

    The premium is carried forward to settlement at the quote currency's
    rate, because it is paid today and the exposure settles later, and
    comparing a cost paid now with one paid then without that step
    understates every option structure.
    """
    carried = upfront * math.exp(quote.r_domestic * quote.expiry)
    hedged_fraction = abs(forward_amount) / exposure.amount
    open_fraction = 1.0 - hedged_fraction
    rate = open_fraction * spot + hedged_fraction * forward_rate
    option_payoff = sum(leg.payoff(spot) for leg in legs)
    # A payable pays the rate and is helped by a positive option payoff; a
    # receivable receives it and is likewise helped. The sign of the premium
    # flips because a payable's "rate" is a cost.
    if exposure.direction is ExposureDirection.PAYABLE:
        return rate - option_payoff + carried
    return rate + option_payoff - carried


def _summarise(
    exposure: Exposure, grid: tuple[float, ...], rates: tuple[float, ...]
) -> tuple[float, float, float]:
    """Worst case, best case and participation over the grid.

    Participation is measured against the unhedged position: what fraction
    of the favourable move the structure keeps. One means fully open in the
    good direction, zero means fully fixed.
    """
    if exposure.direction is ExposureDirection.PAYABLE:
        worst, best = max(rates), min(rates)
        favourable = min(grid)
        unhedged_gain = exposure_spot = None
        del unhedged_gain, exposure_spot
        reference = max(grid)
        open_span = reference - favourable
        kept = rates[grid.index(reference)] - rates[grid.index(favourable)]
    else:
        worst, best = min(rates), max(rates)
        favourable = max(grid)
        reference = min(grid)
        open_span = favourable - reference
        kept = rates[grid.index(favourable)] - rates[grid.index(reference)]
    participation = 0.0 if open_span <= 0.0 else max(0.0, min(1.0, kept / open_span))
    return worst, best, participation


def _structure(
    name: str,
    exposure: Exposure,
    quote: StructureQuote,
    grid: tuple[float, ...],
    *,
    legs: tuple[_Leg, ...] = (),
    forward_amount: float = 0.0,
    forward_rate: float | None = None,
    hedge_ratio: float = 0.0,
) -> StructureResult:
    """Cost one structure and evaluate it across the grid."""
    upfront = sum(leg.quantity * quote.price(leg.strike, leg.kind) for leg in legs)
    outright = quote.forward if forward_rate is None else forward_rate
    rates = tuple(
        _effective_rate(exposure, quote, legs, forward_amount, outright, upfront, spot)
        for spot in grid
    )
    worst, best, participation = _summarise(exposure, grid, rates)
    evidence = Evidence(
        produced_by=f"hedging_structures.{name}",
        fields={
            "structure": name,
            "upfront_cost": upfront,
            "hedge_ratio": hedge_ratio,
            "legs": [
                {"kind": leg.kind.value, "strike": leg.strike, "quantity": leg.quantity}
                for leg in legs
            ],
            "forward_amount": forward_amount,
            "forward_rate": outright,
            "premium_carried_to_settlement": upfront
            * math.exp(quote.r_domestic * quote.expiry),
            **quote.to_dict(),
            **exposure.to_dict(),
        },
    )
    return StructureResult(
        evidence=evidence,
        name=name,
        upfront_cost=upfront,
        total_cost=upfront * exposure.amount,
        worst_case_rate=worst,
        best_case_rate=best,
        upside_participation=participation,
        hedge_ratio=hedge_ratio,
        legs=tuple((leg.kind.value, leg.strike, leg.quantity) for leg in legs),
        forward_amount=forward_amount,
        payoff_grid=rates,
        spot_grid=grid,
    )


@dataclass(frozen=True)
class StructureComparison(EngineResult):
    """Every structure, side by side, and nothing saying which to pick.

    Attributes:
        exposure: What was hedged.
        structures: The rows, in the order they were built.
        spot_grid: The spots every row was evaluated at.
        residual_variance: The variance decomposition for the forward row,
            or ``None`` when no correlation was supplied.
    """

    exposure: Exposure
    structures: tuple[StructureResult, ...]
    spot_grid: tuple[float, ...]
    residual_variance: dict[str, float] | None

    def by_name(self, name: str) -> StructureResult:
        """One row by name.

        Args:
            name: The structure's name.

        Returns:
            Its :class:`StructureResult`.

        Raises:
            KeyError: No such structure.
        """
        for structure in self.structures:
            if structure.name == name:
                return structure
        raise KeyError(
            f"no structure named {name!r}; this comparison holds "
            f"{[s.name for s in self.structures]}"
        )

    def ordered_by_cost(self) -> tuple[StructureResult, ...]:
        """The rows sorted by upfront cost, cheapest first.

        Sorting is not ranking: the cheapest row is the one that protects
        least, which is the whole content of the trade-off.
        """
        return tuple(sorted(self.structures, key=lambda s: s.upfront_cost))

    def payload_fields(self) -> dict[str, Any]:
        """The table, the exposure, and the trade-off as labelled fields."""
        return {
            "exposure": self.exposure.to_dict(),
            "structures": [s.payload_fields() for s in self.structures],
            "spot_grid": list(self.spot_grid),
            "residual_variance": self.residual_variance,
            "trade_off": dict(TRADE_OFF_FRAME),
        }


def _grid(spot: float, points: int, width: float) -> tuple[float, ...]:
    lowest, highest = spot * (1.0 - width), spot * (1.0 + width)
    step = (highest - lowest) / (points - 1)
    return tuple(lowest + step * k for k in range(points))


def compare_structures(
    exposure: Exposure,
    quote: StructureQuote,
    *,
    otm_offset: float = 0.03,
    spread_offset: float = 0.08,
    collar_offset: float = 0.08,
    hedge_ratio: float = 1.0,
    grid_points: int = 21,
    grid_width: float = 0.20,
    correlation: float | None = None,
    foreign_asset_volatility: float | None = None,
) -> StructureComparison:
    """Price seven hedge structures against one exposure and tabulate them.

    Args:
        exposure: What is being hedged.
        quote: The market to strike on.
        otm_offset: How far out of the money the protective wing sits, as a
            fraction of the forward.
        spread_offset: How far out the sold leg of a spread sits.
        collar_offset: How far out, on the *opposite* side, the collar's
            sold wing sits. A separate zero-cost collar is always reported
            alongside it, with the sold strike solved rather than chosen.
        hedge_ratio: Fraction of the exposure the forward row covers, in
            ``[0, 1]``. AC-4.3.
        grid_points: Points in the spot grid, odd so the forward-ish middle
            is included.
        grid_width: Half-width of the grid as a fraction of spot.
        correlation: Correlation between the foreign asset return and the
            exchange rate, for the residual-variance decomposition. ``None``
            skips it rather than assuming zero.
        foreign_asset_volatility: The asset's own volatility, needed with
            ``correlation``.

    Returns:
        The :class:`StructureComparison`.

    Raises:
        ValueError: ``hedge_ratio`` is outside ``[0, 1]``, an offset is not
            positive, or only one of the two variance inputs was given.
    """
    if not 0.0 <= hedge_ratio <= 1.0:
        raise ValueError(f"a hedge ratio is a fraction in [0, 1]; got {hedge_ratio!r}")
    if otm_offset <= 0.0 or spread_offset <= otm_offset:
        raise ValueError(
            f"the spread's sold leg must sit further out than the protective one; "
            f"got otm_offset={otm_offset!r}, spread_offset={spread_offset!r}"
        )
    if collar_offset <= 0.0:
        raise ValueError(f"collar_offset must be positive, got {collar_offset!r}")
    if (correlation is None) != (foreign_asset_volatility is None):
        raise ValueError(
            "the residual-variance decomposition needs both a correlation and the "
            "foreign asset's volatility, or neither. Supplying one and defaulting "
            "the other would report a number nobody asked for."
        )

    grid = _grid(quote.spot, grid_points, grid_width)
    outright = quote.forward
    protective = exposure.direction.protective_kind
    away = 1.0 + otm_offset if protective is OptionKind.CALL else 1.0 - otm_offset
    far = 1.0 + spread_offset if protective is OptionKind.CALL else 1.0 - spread_offset
    atm_strike = outright
    otm_strike = outright * away
    far_strike = outright * far
    signed = exposure.amount * (
        -1.0 if exposure.direction is ExposureDirection.PAYABLE else 1.0
    )

    rows: list[StructureResult] = [
        _structure("unhedged", exposure, quote, grid),
        _structure(
            "forward",
            exposure,
            quote,
            grid,
            forward_amount=signed * hedge_ratio,
            hedge_ratio=hedge_ratio,
        ),
        _structure(
            "protective_option_atm",
            exposure,
            quote,
            grid,
            legs=(_Leg(protective, atm_strike, 1.0),),
        ),
        _structure(
            "protective_option_otm",
            exposure,
            quote,
            grid,
            legs=(_Leg(protective, otm_strike, 1.0),),
        ),
    ]

    # Two collars, because AC-4.2 asks for two different things: one that
    # takes its place in the cost ordering, with a sold wing chosen like the
    # spread's, and one whose sold strike is solved so the net premium is
    # exactly zero. Reporting only the second would leave the ordering with
    # a hole in it; reporting only the first would drop the structure a
    # treasurer most often actually asks for.
    collar_sold = outright * (
        1.0 - collar_offset if protective is OptionKind.CALL else 1.0 + collar_offset
    )
    rows.append(
        _structure(
            "collar",
            exposure,
            quote,
            grid,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective.opposite, collar_sold, -1.0),
            ),
        )
    )

    sold_strike = zero_cost_collar_strike(quote, otm_strike, protective)
    rows.append(
        _structure(
            "collar_zero_cost",
            exposure,
            quote,
            grid,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective.opposite, sold_strike, -1.0),
            ),
        )
    )
    rows.append(
        _structure(
            "option_spread",
            exposure,
            quote,
            grid,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective, far_strike, -1.0),
            ),
        )
    )
    spread_cost = rows[-1].upfront_cost
    seagull_sold = zero_cost_collar_strike(quote, otm_strike, protective)
    rows.append(
        _structure(
            "seagull",
            exposure,
            quote,
            grid,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective, far_strike, -1.0),
                _Leg(protective.opposite, seagull_sold, -1.0),
            ),
        )
    )
    del spread_cost

    decomposition: dict[str, float] | None = None
    if correlation is not None and foreign_asset_volatility is not None:
        # sigma^2(RDC) = sigma^2(RFC) + sigma^2(RFX) + 2 rho sigma(RFC) sigma(RFX)
        asset = float(foreign_asset_volatility)
        currency = float(quote.vol_at(outright))
        cross = 2.0 * float(correlation) * asset * currency
        total = asset * asset + currency * currency + cross
        hedged = total - hedge_ratio * (currency * currency + cross)
        decomposition = {
            "asset_variance": asset * asset,
            "currency_variance": currency * currency,
            "cross_term": cross,
            "total_variance_unhedged": total,
            "total_volatility_unhedged": math.sqrt(max(total, 0.0)),
            "residual_variance_at_hedge_ratio": hedged,
            "residual_volatility_at_hedge_ratio": math.sqrt(max(hedged, 0.0)),
            "hedge_ratio": hedge_ratio,
            "correlation": float(correlation),
        }

    evidence = Evidence(
        produced_by="hedging_structures.compare_structures",
        fields={
            "structures": [s.name for s in rows],
            "protective_kind": protective.value,
            "atm_strike": atm_strike,
            "otm_strike": otm_strike,
            "far_strike": far_strike,
            "collar_sold_strike": collar_sold,
            "collar_zero_cost_sold_strike": sold_strike,
            "hedge_ratio": hedge_ratio,
            "grid_points": grid_points,
            "grid_width": grid_width,
            "residual_variance": decomposition,
            "trade_off": dict(TRADE_OFF_FRAME),
            **quote.to_dict(),
            **exposure.to_dict(),
        },
        sources=tuple(s.evidence for s in rows),
    )
    return StructureComparison(
        evidence=evidence,
        exposure=exposure,
        structures=tuple(rows),
        spot_grid=grid,
        residual_variance=decomposition,
    )
